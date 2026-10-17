"""Shared setup for the access workflow tests (real Cedar, SQLite and TaskManager).

- ``make_agent``: a team lead or worker running one task in the shared manager
  database, with a gateway-signed task context. A delegated worker passes the
  lead's ``signed`` context, as the runtime copies it onto child tasks.
- ``StagedChanges``: staged changes of real catalog change kinds (applied with
  ``apply_change``); only the plan storage is a test double, so the batch and
  ``apply_change`` flows run unchanged against the access store and Cedar.
- ``ToolCall``: a Strands tool context whose interrupt pauses until answered.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands.interrupt import Interrupt, InterruptException

from access_control.changes import ChangeApproval
from access_control.entities import PersonPrincipal
from custom_agents import access_runtime, change_approvals
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import ChangeProposalV1, contract_digest
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
OWNER = PersonPrincipal("user-1", TEAM, ("owner",))
LEAD_TYPE = "TeamArchitect"
WORKER_TYPE = "WebResearchAgent"

needs_cedar = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


def storage_settings(tmp_path: Path) -> Any:
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})


def use_signed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()


def signed_context(person: PersonPrincipal = OWNER) -> Any:
    return context_signer().issue(team_id=TEAM, person_id=person.person_id, person_roles=person.roles)


def make_agent(
    tmp_path: Path,
    *,
    agent_type: str = LEAD_TYPE,
    lead: bool = True,
    task_id: str = "task-lead",
    signed: Any = None,
    enabled: bool = True,
) -> SimpleNamespace:
    """An agent running ``task_id`` for the owner (or for ``signed``'s person)."""

    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity(f"agent-{agent_type}", agent_type, TEAM, agent_type)
    meta = MessageMeta(conversation_id="conv-1")
    meta.gateway_context = SimpleNamespace(task_context=signed or signed_context(), session_key="s-owner")
    manager.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    return SimpleNamespace(
        id=f"agent-{agent_type}",
        name=agent_type,
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role="worker",
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=manager,
        instance_root=None,
        _active_execution_task_id=task_id,
        _interrupted_turn_sessions={},
        paused=False,
        is_executing=False,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        # Limits come from the instance configuration in production; the
        # defaults apply here unless a test loads a configuration.
        _access_workflow_limits_loaded=True,
    )


@dataclass
class StagedChanges:
    """Staged plans of one real catalog change kind (see the module docstring)."""

    kind: str
    prefix: str
    plans: dict[str, str] = field(default_factory=dict)
    applied: dict[str, ChangeApproval] = field(default_factory=dict)

    def stage(self, name: str, content: str = "v1") -> str:
        change_id = f"{self.prefix}{name}"
        self.plans[change_id] = content
        return change_id

    def owns(self, change_id: str) -> bool:
        return change_id.startswith(self.prefix)

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        if change_id not in self.plans:
            raise FileNotFoundError(f"No staged plan {change_id}.")
        if change_id in self.applied:
            raise FileExistsError(f"{change_id} was already applied.")
        return ChangeProposalV1(
            kind=self.kind,
            change_id=change_id,
            plan_digest=contract_digest({"change_id": change_id, "content": self.plans[change_id]}),
            title=f"{self.kind.replace('_', ' ')} {change_id}",
            summary=f"Apply {change_id}.",
            effect=(f"Apply {change_id} ({self.plans[change_id]}).",),
        )

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        self.describe(agent, change_id)
        self.applied[change_id] = approval
        return {"applied": change_id}


def use_staged_changes(monkeypatch: pytest.MonkeyPatch, *changes: StagedChanges) -> None:
    monkeypatch.setattr(change_approvals, "CHANGE_ADAPTERS", tuple(changes))


class ToolCall:
    """A Strands tool context: its interrupt pauses until a response is set."""

    def __init__(self, tool_use_id: str = "tooluse-batch", name: str = "request_access_workflow_approvals") -> None:
        self.tool_use = {"toolUseId": tool_use_id, "name": name}
        self.response: Any = None
        self.reasons: list[dict[str, Any]] = []

    def interrupt(self, name: str, reason: Any = None) -> Any:
        if self.response is not None:
            return self.response
        self.reasons.append(reason)
        raise InterruptException(Interrupt(id=f"interrupt-{self.tool_use['toolUseId']}", name=name, reason=reason))


def run_tool(agent: Any, tool: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call a tool function as the agent (async tools are run to completion)."""

    def call() -> Any:
        set_agent_ref(agent)
        result = tool._tool_func(*args, **kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return json.loads(contextvars.copy_context().run(call))
    # Inside an async test: run the tool's own event loop in another thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return json.loads(pool.submit(contextvars.copy_context().run, call).result())


def proposal(**updates: Any) -> dict[str, Any]:
    """A complete plan: an account, two changes and a tool, the work and a final check."""

    steps = [
        {"step_id": "account", "title": "Choose the account", "kind": "account",
         "reason": "The person keeps the account under their control", "success_criterion": "An account is linked"},
        {"step_id": "specialist", "title": "Create the crawler specialist", "kind": "capability", "depends_on": ["account"],
         "reason": "The crawler work needs its own specialist", "success_criterion": "The specialist is created",
         "change_kinds": ["agent_definition"]},
        {"step_id": "restart", "title": "Restart the team", "kind": "setup", "depends_on": ["account"],
         "reason": "New tools load on restart", "success_criterion": "The team restarted",
         "change_kinds": ["runtime_restart"]},
        {"step_id": "research", "title": "Schedule the weekly research", "kind": "work", "depends_on": ["account"],
         "reason": "The report runs every week", "success_criterion": "The schedule exists",
         "tool_access": [{"tool_id": "fn:schedules", "level": "write"}]},
        {"step_id": "verify", "title": "Check the first report", "kind": "review",
         "depends_on": ["specialist", "restart", "research"],
         "reason": "Confirm the complete result", "success_criterion": "The person has the first report"},
    ]
    return {"title": "Weekly crawler report", "goal": "Send a weekly report built with the crawler",
            "target_agent_type": WORKER_TYPE, "steps": steps, **updates}


__all__ = [
    "BINARY",
    "LEAD_TYPE",
    "OWNER",
    "StagedChanges",
    "TEAM",
    "ToolCall",
    "WORKER_TYPE",
    "make_agent",
    "needs_cedar",
    "proposal",
    "run_tool",
    "signed_context",
    "storage_settings",
    "use_signed_contexts",
    "use_staged_changes",
]
