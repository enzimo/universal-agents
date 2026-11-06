"""Supervised runtime restarts bound to the work they interrupt, applied through ``apply_change``.

TeamArchitect stages a plan that records the runtime state and, from it, the
tasks the restart would interrupt or delay: every request execution root the manager holds
(running, queued or waiting) and, for an ``all`` restart, the work other
agents are running. A person approves the exact plan in the Inbox
(``access_control.changes``), or, with authorization off, the person's
conversational agreement applies it. The plan's digest covers everything the
person was shown; its ``runtime_state_digest`` covers what they agreed the
restart may do: its processes and the tasks it interrupts.

Applying captures the state again and writes a request the supervisor acts
on; the supervisor re-checks that the request records an approval
(``docker/launch_headless``). The approval stays valid while the restart
would interrupt the same tasks or fewer: tasks finish, wait or change state
all the time, and the person agreed to interrupt each of them. It is refused,
and a person must approve a fresh plan, when the restart would also
interrupt a task nobody agreed to, such as a new message or a scheduled run
that arrived after planning. Work an agent runs for a task the manager
delegated belongs to that task, so a specialist moving to the next part of
an approved task changes nothing.
Locally runnable plan steps and recovery tasks count under that same root.

For an ``all`` restart the team lead asks every other live agent what it is
running: the team's records say which agents are live, but not which task
each runs. It sends each a full ``agent.status`` request, all at once, and
waits at most ``TEAM_STATUS_TIMEOUT_SECONDS`` in total, however many agents
there are. An agent that does not answer in time, refuses or cannot be
reached is shown as not answering (``<agent id>:not-answering``), never as
idle: its work is unknown, and the person approves interrupting it as such.
The capture runs in a tool's worker thread (Strands runs synchronous tools
there, and ``apply_change`` describes and applies plans there). The requests
run on a private event loop in that thread and reach the other agents
through this process's host (``custom_agents.agent_requests.ask_agent``),
which runs on the team lead's own loop; that loop stays free meanwhile, so
the capture never waits on a loop it blocks.

A manager restart ends the process running TeamArchitect, so it can
interrupt the task that asked for it before the agent reads the result of
``apply_change``. The task's conversation is kept only in memory, so after
the restart the task would see only the person's message and ask for the
restart again. The receipt therefore names the task that asked for the
restart (``source_task_id``) and the approver's note. The supervisor writes
the receipt from the approved request before stopping the manager; recovery
(``custom_agents.runtime_recovery``) reads it with
``restarts_dispatched_for_task`` and tells the resumed task what happened
(``restart_resume_directive``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, field_validator

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import acting_agent, runtime_access
from custom_agents.agent_requests import ask_agent, team_view
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_json,
    canonical_json_sha256,
    model_digest,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import (
    CHANGE_EFFECT_MAX_CHARS,
    ChangeApprovalMode,
    ChangeProposalV1,
    summary_line,
)
from custom_messages.contracts.common_v1 import utc_now
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_MANAGER,
    RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
    RUNTIME_RESTART_SCOPES,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.runtime_paths import resolve_runtime_instance_root
from custom_agents.skill_repository import verify_sha256
from id_generation import new_id
from runtime_settings.instance_layout import InstanceLayout

if TYPE_CHECKING:
    from custom_transport.protocol1.team_agents import TeamAgent


# Version 4 records, for an ``all`` restart, whether each other agent answered
# the team lead (``RuntimeRestartAgentState.answered``). Version 3 plans took
# other agents' work from what they reported on their own; their digest does
# not cover the new field and their review never said an agent was not
# answering, so they are not loaded and TeamArchitect stages them again. Plans
# last an hour, so only one staged just before an upgrade is affected.
RUNTIME_RESTART_PLAN_SCHEMA_VERSION = 4
RUNTIME_RESTART_PLAN_TTL_SECONDS = 60 * 60
# How long the team lead waits for the other agents' answers when it captures
# an ``all`` restart's impact. It asks them all at once, so this bounds the
# whole capture however many agents there are (module docstring).
TEAM_STATUS_TIMEOUT_SECONDS = 5.0
# Interrupted-work keys for another agent's work that has no task id: it runs
# work without saying which, or it did not answer at all.
UNKNOWN_TASK = "unknown-task"
NOT_ANSWERING = "not-answering"
RUNTIME_RESTART_CHANGE_KIND = "runtime_restart"
_CHANGE_ID_PATTERN = re.compile(r"runtime_restart_[0-9a-f]{12}\Z")
# Scopes that restart the manager process, and so the task that asked.
_SCOPES_INTERRUPTING_MANAGER_TASKS = frozenset({RESTART_SCOPE_MANAGER, RESTART_SCOPE_ALL})
_REASON_PREFIX = "Reason: "
RESTART_REASON_MAX_CHARS = CHANGE_EFFECT_MAX_CHARS - len(_REASON_PREFIX)

logger = logging.getLogger(__name__)


class RuntimeRestartTaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: str
    is_current: bool = False
    subtasks: tuple["RuntimeRestartTaskState", ...] = ()


class RuntimeRestartAgentState(BaseModel):
    """Another live agent, as it answered the team lead's full status request.

    Its id, type and name come from the team's records. When it did not
    answer (``answered`` false), what it runs is unknown: the fields its
    answer fills stay ``None``, never false or zero, so nothing reads it as
    idle, and the restart counts it as ``<agent id>:not-answering``.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    named_agent_type: str = ""
    name: str = ""
    answered: bool
    is_executing: Optional[bool] = None
    current_task_id: str = ""
    current_task_age_seconds: Optional[int] = None
    top_level_queue_depth: Optional[int] = None
    waiting_top_level_task_count: Optional[int] = None
    deferred_message_count: Optional[int] = None

    @property
    def label(self) -> str:
        return self.name or self.agent_id

    @property
    def not_answering_key(self) -> str:
        return f"{self.agent_id}:{NOT_ANSWERING}"


class RuntimeRestartImpactSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at_utc: datetime
    scope: Literal["manager", "gateway", "all"]
    affected_processes: tuple[str, ...]
    manager_agent_id: str
    manager_current_task_id: str
    manager_tracked_top_level_task_count: int
    manager_runnable_top_level_queue_depth: int
    manager_waiting_top_level_task_count: int
    manager_tasks: tuple[RuntimeRestartTaskState, ...]
    known_team_agents: tuple[RuntimeRestartAgentState, ...]
    gateway_session_state: Literal["unavailable_to_agent"] = "unavailable_to_agent"

    def interrupted_work(self) -> dict[str, str]:
        """Each task this restart interrupts or delays, with what it was doing when captured.

        The keys are what a person agrees to interrupt. Every task the
        manager holds counts, whatever its state: the restart stops a running
        one, and a queued or waiting one waits for the restart and continues
        from saved state. Other agents' running work counts for an ``all``
        restart (their list is empty otherwise). Work an agent runs for a
        task the manager delegated counts as that task, so moving to its next
        part adds nothing. An agent that runs work without saying which
        counts as ``<agent id>:unknown-task``, and one that did not answer as
        ``<agent id>:not-answering``: whatever it runs is interrupted.
        """

        work = {item.task_id: item.state for item in self.manager_tasks}
        delegated_by = {child.task_id: item.task_id for item in self.manager_tasks for child in item.subtasks}
        for agent in self.known_team_agents:
            if not agent.answered:
                work[agent.not_answering_key] = f"{agent.label} did not answer, so its work is unknown"
                continue
            if not agent.is_executing:
                continue
            running = agent.current_task_id or f"{agent.agent_id}:{UNKNOWN_TASK}"
            task_id = delegated_by.get(running, running)
            if task_id in work:
                work[task_id] += f", {agent.label} on {running}"
            else:
                work[task_id] = f"running on {agent.label}"
        return dict(sorted(work.items()))

    def agents_not_answering(self) -> tuple[RuntimeRestartAgentState, ...]:
        """The other agents that did not answer the team lead, so their work is unknown."""

        return tuple(agent for agent in self.known_team_agents if not agent.answered)

    def interrupted_tasks(self) -> dict[str, str]:
        """``interrupted_work`` without the agents that did not answer; the review lists those apart."""

        silent = {agent.not_answering_key for agent in self.agents_not_answering()}
        return {task_id: doing for task_id, doing in self.interrupted_work().items() if task_id not in silent}

    def restart_terms(self) -> dict[str, Any]:
        """What the restart does besides interrupting tasks; fixed by its scope and manager."""

        return {
            "scope": self.scope,
            "affected_processes": list(self.affected_processes),
            "manager_agent_id": self.manager_agent_id,
            "gateway_session_state": self.gateway_session_state,
        }

    def approval_payload(self) -> dict[str, Any]:
        """What a person approves the restart to do (``runtime_state_digest`` covers it).

        Task states, counts, queue depths and clocks are left out: they
        describe the moment of planning and change without the restart
        interrupting anything more.
        """

        return {**self.restart_terms(), "interrupted_tasks": list(self.interrupted_work())}


class RuntimeRestartPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = RUNTIME_RESTART_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str
    source_task_id: str
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    scope: Literal["manager", "gateway", "all"]
    reason: str
    impact_snapshot: RuntimeRestartImpactSnapshot
    # Digest of ``impact_snapshot.approval_payload()``: the processes and the
    # tasks the person agreed to interrupt. The supervisor's request and
    # receipt record it with ``plan_digest``.
    runtime_state_digest: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != RUNTIME_RESTART_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"This restart plan has schema version {int(value)}, written by another release; only version "
                f"{RUNTIME_RESTART_PLAN_SCHEMA_VERSION} is supported. Stage a fresh restart plan."
            )
        return int(value)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid runtime restart change id.")
        return normalized

    @field_validator("plan_digest", "runtime_state_digest")
    @classmethod
    def _validate_digests(cls, value: str, info: Any) -> str:
        if info.field_name == "plan_digest" and value == "":
            return value
        return verify_sha256(value, field_name=info.field_name)


@dataclass(frozen=True)
class RuntimeRestartPaths:
    instance_root: Path
    root: Path
    plans: Path
    receipts: Path
    requests: Path

    def request_for_change(self, change_id: str) -> Path:
        """One publication per approved plan, including while it waits."""
        if not _CHANGE_ID_PATTERN.fullmatch(change_id):
            raise ValueError("Invalid runtime restart change id.")
        suffix = change_id.removeprefix(f"{RUNTIME_RESTART_CHANGE_KIND}_")
        return self.requests / f"restart_{suffix}.json"

    def ensure(self) -> None:
        for path in (self.root, self.plans, self.receipts, self.requests):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)


def _state_digest(snapshot: RuntimeRestartImpactSnapshot) -> str:
    """The plan's ``runtime_state_digest``: what the person agreed the restart may do."""

    return canonical_json_sha256(snapshot.approval_payload())


def _new_interrupted_tasks(
    approved: RuntimeRestartImpactSnapshot, current: RuntimeRestartImpactSnapshot
) -> tuple[str, ...]:
    """Tasks the restart would interrupt now that the person did not agree to interrupt."""

    return tuple(sorted(set(current.interrupted_work()) - set(approved.interrupted_work())))


def _outside_approval(approved: RuntimeRestartImpactSnapshot, current: RuntimeRestartImpactSnapshot) -> str:
    """Why the approved restart no longer covers what it would do now; empty while it does.

    The approval holds while the restart interrupts the same tasks or fewer.
    The restart's other terms come from its scope and manager, which the plan
    fixes; they are compared too, so a term added to the approval later is
    never left unchecked.

    An agent that did not answer is its own entry (``<agent id>:not-answering``),
    so for an ``all`` restart:

    - an agent that answered when planned and does not answer now is refused:
      the approval named what it was doing, and it may now run a task nobody
      agreed to interrupt;
    - an agent that did not answer when planned and now reports a task is
      refused, unless the task is part of an approved one: nobody can tell
      whether it started after planning; reporting no work keeps the approval;
    - an agent that did not answer either time keeps the approval: the person
      agreed to interrupt whatever it runs. Refusing it would leave no way to
      restart a team with an agent that hangs, which is when a restart is
      most needed.
    """

    if current.restart_terms() != approved.restart_terms():
        return "What this restart affects changed after planning."
    new = _new_interrupted_tasks(approved, current)
    if not new:
        return ""
    silent = {agent.not_answering_key: agent.label for agent in current.agents_not_answering()}
    started = [task_id for task_id in new if task_id not in silent]
    stopped_answering = [silent[task_id] for task_id in new if task_id in silent]
    why = []
    if started:
        why.append(f"New work started that this restart would interrupt: {', '.join(started)}.")
    if stopped_answering:
        why.append(
            f"These agents do not answer now, so the restart may interrupt work nobody approved: "
            f"{', '.join(stopped_answering)}."
        )
    why.append("The approval covers only the work listed when the restart was planned.")
    return " ".join(why)


def resolve_runtime_restart_paths(agent: Any) -> RuntimeRestartPaths:
    instance_root = resolve_runtime_instance_root(
        bootstrap_config=dict(getattr(agent, "bootstrap_config", {}) or {}),
        instance_config_path=getattr(agent, "instance_config_path", None),
        instance_root=getattr(agent, "instance_root", None),
        workspace_dir=getattr(agent, "workspace_dir", None),
    )
    if not instance_root:
        raise RuntimeError("Runtime restart requires a resolvable runtime instance root.")
    layout = InstanceLayout.at(instance_root)
    authorization_root = layout.runtime_restart_authorization_dir
    return RuntimeRestartPaths(
        instance_root=layout.root,
        root=authorization_root,
        plans=authorization_root / "plans",
        receipts=authorization_root / "receipts",
        requests=layout.runtime_restart_requests_dir,
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "unknown")


def _task_state(task_info: Any, *, current_task_id: str, task_manager: Any) -> RuntimeRestartTaskState:
    task_id = str(getattr(getattr(task_info, "task", None), "task_id", "") or "").strip()
    # Plan steps and recovery tasks are locally runnable top-level tasks, but
    # still belong to their original execution root. Approve that request,
    # not whichever replaceable step happens to be queued when it is staged.
    root_id = task_manager.resolve_root_task_id(task_id) or task_id
    root_info = task_manager.get_task_info(root_id) or task_info
    # Keep the complete execution graph, flattened under its root. A worker
    # can start a descendant of an approved child without interrupting a new
    # request. The manager's graph resolver already handles parent cycles.
    children = (
        child for child in task_manager.get_execution_graph_task_infos(task_id)
        if str(getattr(getattr(child, "task", None), "task_id", "") or "") != root_id
    )
    return RuntimeRestartTaskState(
        task_id=root_id,
        state=_enum_value(getattr(root_info, "state", "unknown")),
        is_current=root_id == current_task_id,
        subtasks=tuple(
            sorted(
                (
                    RuntimeRestartTaskState(
                        task_id=str(getattr(getattr(child, "task", None), "task_id", "") or ""),
                        state=_enum_value(getattr(child, "state", "unknown")),
                        is_current=child.task.task_id == current_task_id,
                    )
                    for child in children
                ),
                key=lambda item: item.task_id,
            )
        ),
    )


class TeamUnseen(RuntimeError):
    """This process cannot see the team's other agents, so an ``all`` restart's impact is unknown."""


_TEAM_UNSEEN = (
    "The team lead cannot see the team's other agents yet (its process has not joined the team's "
    "messaging), so it cannot tell what an 'all' restart would interrupt. Try again in a minute; "
    "a 'manager' restart does not need this."
)


def _other_agents_now(agent: Any) -> tuple[RuntimeRestartAgentState, ...]:
    """Every other live agent, as it answers the team lead now (module docstring).

    The live agents come from this process's view of the team's records. The
    team lead, ``agent``, is not asked: its own tasks come from its task
    manager. Without the view the team is unknown, so an ``all`` restart is
    refused rather than shown as interrupting no other agent's work.
    """

    view = team_view()
    if view is None:
        logger.warning(
            "An 'all' restart's impact was not captured: this process cannot see the team's agents. "
            "event=runtime_restart_team_unseen agent_id=%s",
            getattr(agent, "id", None),
        )
        raise TeamUnseen(_TEAM_UNSEEN)
    others = sorted(
        (item for item in view.live().values() if item.agent != str(agent.id)), key=lambda item: item.agent
    )
    answers = _ask_full_statuses(agent, others) if others else {}
    return tuple(_agent_state(item, answers.get(item.agent)) for item in others)


def _ask_full_statuses(agent: Any, others: Sequence[TeamAgent]) -> dict[str, dict[str, Any]]:
    """Each answer to the team lead's full ``agent.status`` request, by agent id; silent agents are left out.

    Runs the requests on a private loop in this worker thread. On an event
    loop it would block that loop, and on the team lead's own loop, which
    carries the requests, it could never finish.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_full_statuses(agent, others))
    logger.error(
        "A restart's impact was captured on an event loop instead of a worker thread. "
        "event=runtime_restart_capture_on_event_loop agent_id=%s",
        getattr(agent, "id", None),
    )
    raise RuntimeError("A restart's impact is captured from a tool's worker thread, never on an event loop.")


async def _full_statuses(agent: Any, others: Sequence[TeamAgent]) -> dict[str, dict[str, Any]]:
    asks = {
        item.agent: asyncio.ensure_future(
            ask_agent(agent, "agent.status", {"level": "full"}, item.agent, timeout=TEAM_STATUS_TIMEOUT_SECONDS)
        )
        for item in others
    }
    # One deadline for all of them, whatever the requests' own timeouts do.
    _done, late = await asyncio.wait(asks.values(), timeout=TEAM_STATUS_TIMEOUT_SECONDS)
    for ask in late:
        ask.cancel()
    await asyncio.gather(*late, return_exceptions=True)
    answers: dict[str, dict[str, Any]] = {}
    for agent_id, ask in asks.items():
        error = None if ask.cancelled() else ask.exception()
        if ask.cancelled():
            reason = f"no answer within {TEAM_STATUS_TIMEOUT_SECONDS:g} s"
        elif error is not None:
            reason = f"{type(error).__name__}: {error}"
        elif ask.result().get("outcome") != "ok":
            reason = f"answered {ask.result().get('outcome')}"
        else:
            answers[agent_id] = ask.result()
            continue
        logger.warning(
            "Another agent did not answer the team lead's status request; the restart shows it as not answering. "
            "event=runtime_restart_agent_not_answering agent_id=%s target_agent_id=%s reason=%s",
            getattr(agent, "id", None),
            agent_id,
            reason,
        )
    return answers


def _agent_state(item: TeamAgent, answer: Optional[dict[str, Any]]) -> RuntimeRestartAgentState:
    """One other agent from its record and its answer (``None`` when it did not answer)."""

    known = {"agent_id": item.agent, "named_agent_type": item.agent_type, "name": item.display_name or ""}
    if answer is None:
        return RuntimeRestartAgentState(**known, answered=False)
    return RuntimeRestartAgentState(
        **known,
        answered=True,
        is_executing=bool(answer["executing"]),
        current_task_id=str(answer.get("current_task_id") or ""),
        current_task_age_seconds=answer.get("current_task_age_seconds"),
        top_level_queue_depth=int(answer["queued"]),
        waiting_top_level_task_count=int(answer["waiting"]),
        deferred_message_count=int(answer["deferred"]),
    )


def capture_runtime_restart_impact(agent: Any, scope: str) -> RuntimeRestartImpactSnapshot:
    """The runtime state a restart of ``scope`` would interrupt, as of now.

    Call it from a worker thread, never on an event loop: an ``all`` restart
    waits up to ``TEAM_STATUS_TIMEOUT_SECONDS`` for the other agents' answers
    (module docstring).
    """

    normalized_scope = str(scope or RESTART_SCOPE_MANAGER).strip().lower()
    if normalized_scope not in RUNTIME_RESTART_SCOPES:
        raise ValueError("restart scope must be 'manager', 'gateway', or 'all'.")
    # The other agents are asked before the manager's tasks are read: work the
    # manager delegated is recorded there before an agent can report it, so
    # each reported part of a manager task finds its task below.
    known_agents = _other_agents_now(agent) if normalized_scope == RESTART_SCOPE_ALL else ()
    task_manager = getattr(agent, "task_manager", None)
    current_task_id = str(
        (task_manager.current_task_id if task_manager is not None else '') or ""
    ).strip()
    get_task_infos = getattr(task_manager, "get_top_level_task_infos", None)
    task_infos = tuple(get_task_infos() or ()) if callable(get_task_infos) else ()
    roots: dict[str, RuntimeRestartTaskState] = {}
    for task_info in task_infos:
        state = _task_state(task_info, current_task_id=current_task_id, task_manager=task_manager)
        roots[state.task_id] = state
    tasks = tuple(roots[task_id] for task_id in sorted(roots))

    def count(method_name: str, fallback: int) -> int:
        method = getattr(task_manager, method_name, None)
        return int(method()) if callable(method) else fallback

    affected = {
        "manager": ("manager",),
        "gateway": ("gateway",),
        "all": (
            "installation_authority",
            "team_services",
            "manager",
            "configured_startup_agents",
            "activity_collector",
            "gateway",
            "execution_controller_if_enabled",
        ),
    }[normalized_scope]
    return RuntimeRestartImpactSnapshot(
        captured_at_utc=utc_now(),
        scope=normalized_scope,
        affected_processes=affected,
        manager_agent_id=str(getattr(agent, "id", "") or ""),
        manager_current_task_id=current_task_id,
        manager_tracked_top_level_task_count=count(
            "get_tracked_top_level_task_count", len(tasks)
        ),
        manager_runnable_top_level_queue_depth=count(
            "get_runnable_top_level_queue_depth", 0
        ),
        manager_waiting_top_level_task_count=count(
            "get_waiting_top_level_task_count",
            sum(item.state.casefold() == "waiting" for item in tasks),
        ),
        manager_tasks=tasks,
        known_team_agents=known_agents,
    )


_APPROVAL_COVERS_TASKS = (
    "This approval covers only the tasks listed. If some of them finish first, the restart still goes "
    "ahead. If another task starts that the restart would interrupt, the restart is refused and a "
    "person must approve it again."
)


_APPROVAL_COVERS_AGENTS = (
    "For the other agents it covers what each was doing when planned. If an agent that answered stops "
    "answering, or one that did not answer reports a task not listed, the restart is refused too."
)


def _restart_summary(plan: RuntimeRestartPlan, processes: str) -> str:
    """The restart in one line for chat: what restarts, how many tasks it interrupts, who did not answer."""

    count = len(plan.impact_snapshot.interrupted_tasks())
    silent = len(plan.impact_snapshot.agents_not_answering())
    if count:
        work = f"{count} task{'s' if count != 1 else ''} may be interrupted or delayed"
    elif silent:
        work = "no task is known to be running, queued or waiting"
    else:
        work = "no tasks are running, queued or waiting"
    if silent:
        work += (
            f"; {silent} agent{'s' if silent != 1 else ''} did not answer, so "
            f"{'their' if silent != 1 else 'its'} work is unknown"
        )
    return summary_line(f"Restart {processes}; {work}.")


def _one_review_line(entries: list[str], line: Callable[[str], str], noun: str) -> str:
    """``line`` naming as many ``entries`` as fit in one review line; the rest are counted, so the count stays exact."""

    shown = len(entries)
    while True:
        names = ", ".join(entries[:shown])
        left_out = len(entries) - shown
        if left_out:
            names = f"{names}, and {left_out} more" if names else f"{left_out} {noun}"
        text = line(names)
        if not shown or len(text) <= CHANGE_EFFECT_MAX_CHARS:
            return text
        shown -= 1


def _impact_summary(plan: RuntimeRestartPlan) -> str:
    """The tasks the person agrees to interrupt, as one review line.

    Names every task while they fit in the line; the count is always exact.
    Agents that did not answer are listed apart (``_not_answering_summary``).
    """

    work = plan.impact_snapshot.interrupted_tasks()
    if not work:
        if plan.impact_snapshot.agents_not_answering():
            return "No task is known to be running, queued or waiting, but some agents did not answer."
        return "No tasks are running, queued or waiting, so the restart interrupts none."
    entries = [f"{task_id} ({doing})" for task_id, doing in work.items()]
    return _one_review_line(
        entries,
        lambda names: f"Tasks it may interrupt or delay ({len(entries)}), with their state when planned: {names}.",
        "tasks",
    )


def _not_answering_summary(plan: RuntimeRestartPlan) -> str:
    """The other agents that did not answer, as one review line; empty when every agent answered."""

    agents = plan.impact_snapshot.agents_not_answering()
    if not agents:
        return ""
    entries = [f"{agent.name} ({agent.agent_id})" if agent.name else agent.agent_id for agent in agents]
    return _one_review_line(
        entries,
        lambda names: (
            f"Agents that did not answer when asked what they are running ({len(entries)}): {names}. "
            "Their work is unknown; the restart interrupts whatever they are running."
        ),
        "agents",
    )


def _review_lines(plan: RuntimeRestartPlan) -> tuple[str, ...]:
    """What the restart interrupts, for the person: the tasks, then the agents that did not answer."""

    silent = _not_answering_summary(plan)
    return (_impact_summary(plan), silent) if silent else (_impact_summary(plan),)


class RuntimeRestartService:
    """Stage and dispatch one exact restart against stable state."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    @staticmethod
    def _plan_store(paths: RuntimeRestartPaths) -> FilePlanStore[RuntimeRestartPlan]:
        return FilePlanStore(
            paths.plans,
            RuntimeRestartPlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid runtime restart change id.",
            missing_plan_message="Runtime restart plan was not found: {change_id}",
        )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    def stage(self, *, scope: str = RESTART_SCOPE_MANAGER, reason: str) -> dict[str, Any]:
        actor = acting_agent(self.agent)
        if actor.agent.agent_type != TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError("Only TeamArchitect may request a runtime restart.")
        if not actor.person_id:
            raise PermissionError("A runtime restart must be staged while TeamArchitect works for a person.")
        if not actor.task_id:
            raise PermissionError("Runtime restart must be staged from an active task.")
        normalized_reason = " ".join(str(reason or "").split())
        if not normalized_reason:
            raise ValueError("Runtime restart reason must not be empty.")
        if len(normalized_reason) > RESTART_REASON_MAX_CHARS:
            raise ValueError(
                f"Runtime restart reason must be at most {RESTART_REASON_MAX_CHARS} characters "
                "so the complete reason fits in the approval review."
            )
        snapshot = capture_runtime_restart_impact(self.agent, scope)
        now = utc_now()
        plan = RuntimeRestartPlan(
            change_id=new_id("runtime_restart"),
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=RUNTIME_RESTART_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=actor.task_id,
            authority_mode=self._authority_mode(),
            scope=snapshot.scope,
            reason=normalized_reason,
            impact_snapshot=snapshot,
            runtime_state_digest=_state_digest(snapshot),
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        paths = resolve_runtime_restart_paths(self.agent)
        paths.ensure()
        self._plan_store(paths).save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation="runtime_restart",
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            "status": "staged",
            **plan.model_dump(mode="json"),
            "user_impact_summary": " ".join(_review_lines(plan)),
            "next_step": (
                "Show the state and impact to the person, then call apply_change with this change_id"
                + (
                    "; a person approves the exact restart in the Inbox."
                    if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                    else " and confirmed=true once they agree."
                )
            ),
        }

    def load_plan(self, change_id: str) -> RuntimeRestartPlan:
        paths = resolve_runtime_restart_paths(self.agent)
        plan = self._plan_store(paths).load(str(change_id or "").strip())
        actor = acting_agent(self.agent)
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("Runtime restart plan digest mismatch.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("Runtime restart plan has expired. Stage a fresh plan.")
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning. Stage a fresh plan.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
            or plan.source_task_id != (actor.task_id or "")
        ):
            raise PermissionError("Runtime restart plan belongs to another agent, person or task.")
        return plan

    def _verify_current(self, plan: RuntimeRestartPlan, paths: RuntimeRestartPaths) -> None:
        """The restart can still run: not dispatched yet, and it interrupts only approved tasks.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a restart that can only be refused.
        """

        if (paths.receipts / f"{plan.change_id}.json").is_file():
            raise FileExistsError(f"Runtime restart plan {plan.change_id} was already dispatched.")
        if paths.request_for_change(plan.change_id).is_file():
            raise FileExistsError(f"Runtime restart plan {plan.change_id} already awaits the supervisor.")
        current_snapshot = capture_runtime_restart_impact(self.agent, plan.scope)
        why = _outside_approval(plan.impact_snapshot, current_snapshot)
        if why:
            logger.info(
                "A restart would now interrupt work its approval does not cover. "
                "event=runtime_restart_outside_approval change_id=%s task_id=%s new_task_ids=%s",
                plan.change_id,
                plan.source_task_id,
                ",".join(_new_interrupted_tasks(plan.impact_snapshot, current_snapshot)),
            )
            raise PermissionError(f"{why} Inspect the new state and stage a fresh restart plan.")

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan, resolve_runtime_restart_paths(self.agent))
        processes = ", ".join(plan.impact_snapshot.affected_processes) or plan.scope
        return ChangeProposalV1(
            kind=RUNTIME_RESTART_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Restart {processes}",
            summary=_restart_summary(plan, processes),
            effect=(
                f"Restarts the supervised processes: {processes}. It does not reboot the host or container.",
                *_review_lines(plan),
                "Gateway session count is unavailable to the agent; connected sessions may need to "
                "reconnect. Active work may be interrupted and recovery depends on persisted state.",
                f"{_REASON_PREFIX}{plan.reason}",
                _APPROVAL_COVERS_TASKS,
                *((_APPROVAL_COVERS_AGENTS,) if plan.scope == RESTART_SCOPE_ALL else ()),
            ),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Dispatch an approved restart (only the change mechanism calls this)."""

        paths = resolve_runtime_restart_paths(self.agent)
        paths.ensure()
        normalized = str(change_id or "").strip()
        receipt_path = paths.receipts / f"{normalized}.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            return {
                "error": "Runtime restart plan was already dispatched.",
                "reason_code": "runtime_restart_replay",
                "change_id": normalized,
                "receipt": receipt,
            }
        plan = self.load_plan(normalized)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This restart needs a person's approval, not agreement (or the reverse).")
        self._verify_current(plan, paths)
        request_path = paths.request_for_change(plan.change_id)
        request_id = request_path.stem
        authority = {
            "mode": plan.authority_mode,
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "runtime_state_digest": plan.runtime_state_digest,
            "approval": approval.record(),
        }
        request_payload = {
            "schema_version": RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "created_at": utc_now().isoformat(),
            "scope": plan.scope,
            "reason": plan.reason,
            "requested_by": plan.actor_principal_id,
            "authority": authority,
            "impact_snapshot": plan.impact_snapshot.model_dump(mode="json"),
            "source_task_id": plan.source_task_id,
            "approval_note": approval.note,
        }
        # Publish recovery metadata with the request in one atomic write.
        # Only the supervisor knows which requests it has accepted; writing
        # a dispatch receipt here would claim a restart that may never run.
        try:
            atomic_write_json(request_path, request_payload)
        except Exception:
            logger.exception(
                "Could not write the runtime restart request; nothing was sent. "
                "event=runtime_restart_request_write_failed change_id=%s",
                plan.change_id,
            )
            raise
        self.audit.record(
            GovernanceAuditEvent(
                operation="runtime_restart",
                phase="applied",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            "status": "restart_requested",
            "change_id": plan.change_id,
            "request_id": request_id,
            "request_path": str(request_path),
            "note": (
                "The supervisor will restart only the approved supervised process scope. "
                "It does not reboot the host or container."
            ),
        }

    def status(self, change_id: str) -> dict[str, Any]:
        paths = resolve_runtime_restart_paths(self.agent)
        normalized = str(change_id or "").strip()
        receipt_path = paths.receipts / f"{normalized}.json"
        if receipt_path.is_file():
            return {"status": "dispatched", "receipt": json.loads(receipt_path.read_text(encoding="utf-8"))}
        request_path = paths.request_for_change(normalized)
        if request_path.is_file():
            return {"status": "requested", "request": json.loads(request_path.read_text(encoding="utf-8"))}
        plan = self._plan_store(paths).load(normalized)
        state = "pending" if plan.expires_at_utc > utc_now() else "expired"
        try:
            current_snapshot = capture_runtime_restart_impact(self.agent, plan.scope)
        except TeamUnseen as unseen:
            # Applying now is refused for the same reason (``_verify_current``).
            return {
                "status": state,
                "plan": plan.model_dump(mode="json"),
                "still_applies": False,
                "refusal_reason": str(unseen),
                "new_interrupted_tasks": [],
                "current_impact_snapshot": None,
            }
        why = _outside_approval(plan.impact_snapshot, current_snapshot)
        return {
            "status": state,
            "plan": plan.model_dump(mode="json"),
            # Whether the approval still covers the restart; see _outside_approval.
            "still_applies": not why,
            "refusal_reason": why or None,
            "new_interrupted_tasks": list(_new_interrupted_tasks(plan.impact_snapshot, current_snapshot)),
            "current_impact_snapshot": current_snapshot.model_dump(mode="json"),
        }


class DispatchedRestart(BaseModel):
    """A restart a task asked for that was sent to the supervisor (from its receipt)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    change_id: str
    scope: str
    reason: str
    dispatched_at_utc: datetime
    source_task_id: str
    approval_note: str = ""
    approved_at_utc: Optional[datetime] = None


def restarts_dispatched_for_task(agent: Any, task_id: str) -> list[DispatchedRestart]:
    """Restarts of the manager this task asked for and that were sent, oldest first.

    Receipts without ``source_task_id`` (written before it existed) or that
    cannot be read are skipped; an unreadable one is logged.
    """

    if not task_id:
        return []
    receipts = resolve_runtime_restart_paths(agent).receipts
    if not receipts.is_dir():
        return []
    found: list[DispatchedRestart] = []
    for path in sorted(receipts.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("source_task_id") != task_id:
                continue
            approval = (payload.get("authority") or {}).get("approval") or {}
            restart = DispatchedRestart.model_validate(
                {**payload, "approved_at_utc": approval.get("approved_at_utc")}
            )
        except (OSError, ValueError, AttributeError):
            logger.warning(
                "Could not read a runtime restart receipt. event=runtime_restart_receipt_unreadable "
                "path=%s task_id=%s",
                path,
                task_id,
                exc_info=True,
            )
            continue
        if restart.scope in _SCOPES_INTERRUPTING_MANAGER_TASKS:
            found.append(restart)
    return sorted(found, key=lambda item: item.dispatched_at_utc)


def restart_resume_directive(restarts: list[DispatchedRestart]) -> str:
    """What the task that asked for these restarts reads when it resumes."""

    lines = [
        "The manager restarted while you worked on this task, because of a restart this task "
        "asked for and a person approved. That restart is done. Your conversation from before "
        "the restart is gone; this is what happened:"
    ]
    for item in restarts:
        approved = (
            f"approved at {item.approved_at_utc.isoformat()}, " if item.approved_at_utc is not None else ""
        )
        note = f' The approver wrote: "{item.approval_note}".' if item.approval_note.strip() else ""
        lines.append(
            f"- {item.change_id}: restart of {item.scope}, {approved}sent at "
            f"{item.dispatched_at_utc.isoformat()}. Reason: {item.reason.rstrip('.')}.{note}"
        )
    if len(restarts) > 1:
        lines.append(
            f"This task has restarted the manager {len(restarts)} times. Do not ask for another restart."
        )
    lines.append(
        "Do not stage or apply another restart for the same reason. Check whether the restart had "
        "the effect you wanted (for example, read again the settings it was meant to load), then "
        "tell the person what you found. Stage a new restart only if the person asks for one after "
        "seeing that."
    )
    return "\n".join(lines)
