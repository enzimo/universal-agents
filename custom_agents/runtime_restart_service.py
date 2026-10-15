"""State-bound supervised runtime restarts, applied through ``apply_change``.

TeamArchitect stages a plan bound to the current runtime state (tasks,
queues, agents). A person approves the exact restart in the Inbox
(``access_control.changes``), or, with authorization off, the person's
conversational agreement applies it. Applying re-checks the state digest and
writes a request the supervisor acts on; the supervisor re-checks that the
request records an approval (``docker/launch_headless``).

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

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import acting_agent, runtime_access
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_json,
    canonical_json_sha256,
    model_digest,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
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


RUNTIME_RESTART_PLAN_SCHEMA_VERSION = 2
# The runtime state digest refuses a restart once tasks or agents changed.
RUNTIME_RESTART_PLAN_TTL_SECONDS = 60 * 60
RUNTIME_RESTART_CHANGE_KIND = "runtime_restart"
_CHANGE_ID_PATTERN = re.compile(r"runtime_restart_[0-9a-f]{12}\Z")
# Scopes that restart the manager process, and so the task that asked.
_SCOPES_INTERRUPTING_MANAGER_TASKS = frozenset({RESTART_SCOPE_MANAGER, RESTART_SCOPE_ALL})

logger = logging.getLogger(__name__)


class RuntimeRestartTaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: str
    is_current: bool = False
    subtasks: tuple["RuntimeRestartTaskState", ...] = ()


class RuntimeRestartAgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    named_agent_type: str = ""
    name: str = ""
    is_executing: bool = False
    current_task_id: str = ""
    current_task_age_seconds: Optional[int] = None
    top_level_queue_depth: int = 0
    waiting_top_level_task_count: int = 0
    deferred_message_count: int = 0
    last_seen_at: Optional[str] = None


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

    def material_payload(self) -> dict[str, Any]:
        """Return restart-relevant state without observational clock fields."""

        return {
            "scope": self.scope,
            "affected_processes": list(self.affected_processes),
            "manager_agent_id": self.manager_agent_id,
            "manager_current_task_id": self.manager_current_task_id,
            "manager_tracked_top_level_task_count": self.manager_tracked_top_level_task_count,
            "manager_runnable_top_level_queue_depth": self.manager_runnable_top_level_queue_depth,
            "manager_waiting_top_level_task_count": self.manager_waiting_top_level_task_count,
            "manager_tasks": [item.model_dump(mode="json") for item in self.manager_tasks],
            "known_team_agents": [
                {
                    "agent_id": item.agent_id,
                    "named_agent_type": item.named_agent_type,
                    "name": item.name,
                    "is_executing": item.is_executing,
                    "current_task_id": item.current_task_id,
                    "top_level_queue_depth": item.top_level_queue_depth,
                    "waiting_top_level_task_count": item.waiting_top_level_task_count,
                    "deferred_message_count": item.deferred_message_count,
                }
                for item in self.known_team_agents
            ],
            "gateway_session_state": self.gateway_session_state,
        }


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
    runtime_state_digest: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != RUNTIME_RESTART_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"Only runtime restart plan schema version {RUNTIME_RESTART_PLAN_SCHEMA_VERSION} is supported."
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
    return canonical_json_sha256(snapshot.material_payload())


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
    get_subtasks = getattr(task_manager, "get_subtasks", None)
    children = tuple(get_subtasks(task_id) or ()) if callable(get_subtasks) and task_id else ()
    return RuntimeRestartTaskState(
        task_id=task_id,
        state=_enum_value(getattr(task_info, "state", "unknown")),
        is_current=task_id == current_task_id,
        subtasks=tuple(
            sorted(
                (
                    RuntimeRestartTaskState(
                        task_id=str(getattr(getattr(child, "task", None), "task_id", "") or ""),
                        state=_enum_value(getattr(child, "state", "unknown")),
                        is_current=False,
                    )
                    for child in children
                ),
                key=lambda item: item.task_id,
            )
        ),
    )


def capture_runtime_restart_impact(agent: Any, scope: str) -> RuntimeRestartImpactSnapshot:
    normalized_scope = str(scope or RESTART_SCOPE_MANAGER).strip().lower()
    if normalized_scope not in RUNTIME_RESTART_SCOPES:
        raise ValueError("restart scope must be 'manager', 'gateway', or 'all'.")
    task_manager = getattr(agent, "task_manager", None)
    current_task_id = str(
        (task_manager.current_task_id if task_manager is not None else '') or ""
    ).strip()
    get_task_infos = getattr(task_manager, "get_top_level_task_infos", None)
    task_infos = tuple(get_task_infos() or ()) if callable(get_task_infos) else ()
    tasks = tuple(
        sorted(
            (
                _task_state(
                    task_info,
                    current_task_id=current_task_id,
                    task_manager=task_manager,
                )
                for task_info in task_infos
            ),
            key=lambda item: item.task_id,
        )
    )

    def count(method_name: str, fallback: int) -> int:
        method = getattr(task_manager, method_name, None)
        return int(method()) if callable(method) else fallback

    known_agents: tuple[RuntimeRestartAgentState, ...] = ()
    if normalized_scope == "all":
        statuses = dict(
            getattr(getattr(agent, "known_agents", None), "agent_map", {}) or {}
        )
        known_agents = tuple(
            sorted(
                (
                    RuntimeRestartAgentState(
                        agent_id=str(getattr(status, "self_id", None) or agent_id),
                        named_agent_type=str(getattr(status, "named_agent_type", None) or ""),
                        name=str(getattr(status, "name", None) or ""),
                        is_executing=bool(getattr(status, "is_executing", False)),
                        current_task_id=str(getattr(status, "current_task_id", None) or ""),
                        current_task_age_seconds=getattr(status, "current_task_age_seconds", None),
                        top_level_queue_depth=int(getattr(status, "top_level_queue_depth", 0) or 0),
                        waiting_top_level_task_count=int(
                            getattr(status, "waiting_top_level_task_count", 0) or 0
                        ),
                        deferred_message_count=int(
                            getattr(status, "deferred_message_count", 0) or 0
                        ),
                        last_seen_at=getattr(status, "last_seen_at", None),
                    )
                    for agent_id, status in statuses.items()
                ),
                key=lambda item: item.agent_id,
            )
        )

    affected = {
        "manager": ("manager",),
        "gateway": ("gateway",),
        "all": (
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


def _restart_summary(plan: RuntimeRestartPlan, processes: str) -> str:
    """The restart in one line for chat: what restarts and how much running work it interrupts."""

    count = plan.impact_snapshot.manager_tracked_top_level_task_count
    work = f"{count} running task{'s' if count != 1 else ''} may be interrupted" if count else "no tasks are running"
    return summary_line(f"Restart {processes}; {work}.")


def _impact_summary(plan: RuntimeRestartPlan) -> str:
    snapshot = plan.impact_snapshot
    task_states = ", ".join(
        f"{item.task_id} ({item.state})" for item in snapshot.manager_tasks
    ) or "none"
    known_executing = [
        item.current_task_id or f"{item.agent_id}:unknown-task"
        for item in snapshot.known_team_agents
        if item.is_executing
    ]
    known_summary = ", ".join(known_executing) or "none"
    return (
        f"Restart supervised scope {plan.scope}; affected processes: "
        f"{', '.join(snapshot.affected_processes)}. Manager state: current task "
        f"{snapshot.manager_current_task_id or 'none'}, "
        f"{snapshot.manager_tracked_top_level_task_count} tracked top-level task(s), "
        f"{snapshot.manager_runnable_top_level_queue_depth} runnable queued, "
        f"{snapshot.manager_waiting_top_level_task_count} waiting. Top-level task states: "
        f"{task_states}. Team-visible executing tasks included for scope=all: {known_summary}. "
        "Gateway session count is unavailable to the agent; connected sessions may need to "
        "reconnect. Active work may be interrupted and recovery depends on persisted state."
    )[:2000]


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
            "user_impact_summary": _impact_summary(plan),
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
        """The restart can still run: not dispatched yet, and the running tasks and agents unchanged.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a restart that can only be refused.
        """

        if (paths.receipts / f"{plan.change_id}.json").is_file():
            raise FileExistsError(f"Runtime restart plan {plan.change_id} was already dispatched.")
        if paths.request_for_change(plan.change_id).is_file():
            raise FileExistsError(f"Runtime restart plan {plan.change_id} already awaits the supervisor.")
        current_snapshot = capture_runtime_restart_impact(self.agent, plan.scope)
        if _state_digest(current_snapshot) != plan.runtime_state_digest:
            raise PermissionError(
                "Material runtime state changed after planning; inspect the new state and "
                "stage a fresh restart plan."
            )

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
                _impact_summary(plan),
                f"Reason: {plan.reason}",
                "If the running tasks or agents change before approval, the restart is refused and must be staged again.",
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
        current_snapshot = capture_runtime_restart_impact(self.agent, plan.scope)
        current_digest = _state_digest(current_snapshot)
        return {
            "status": "pending" if plan.expires_at_utc > utc_now() else "expired",
            "plan": plan.model_dump(mode="json"),
            "current_runtime_state_digest": current_digest,
            "runtime_state_unchanged": current_digest == plan.runtime_state_digest,
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
