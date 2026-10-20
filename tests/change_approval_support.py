"""Shared fakes for tests that stage a change and apply it with ``apply_change``.

Platform services bind a staged plan to the acting agent and to the person its
task works for, read only from the task's signed context
(``custom_agents.access_runtime.acting_agent``). These helpers give tests:

- a task manager whose running task carries a gateway-signed context;
- a TeamArchitect (or specialist) agent with authorization on (real Cedar and
  the SQLite access store in the manager database) or off;
- a Strands tool context that pauses like the real interrupt until a decision
  is set, and helpers to run ``apply_change`` and to approve its Inbox request.
"""

from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import Interrupt, InterruptException

from access_control.changes import ChangeApproval
from access_control.entities import PersonPrincipal
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import AccessRequestV1, AccessReviewChoice, ChangeApprovalMode
from custom_tools.providers.changes import apply_change
from custom_tools.runtime_context import set_agent_ref
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys
from tests.tool_result_support import tool_json

ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
needs_cedar = pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
TASK_ID = "task_change"
OWNER_ROLES = ("owner",)


def use_signed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the test gateway key and forget identity/access caches from earlier tests."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    forget_task_context_keys()


def authorization_config(enabled: bool) -> dict[str, Any]:
    if enabled:
        return {"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)}
    return {"enabled": False}


def agreement() -> ChangeApproval:
    """What apply_change passes to a service with authorization off and confirmed=true."""

    return ChangeApproval(ChangeApprovalMode.AGREEMENT, "agreement", datetime.now(timezone.utc))


class SignedTaskManager:
    """The manager of one running task a person asked for through the gateway.

    ``current_task_id`` may be changed to act from another task of the same
    person (plans must continue in the task that staged them).
    """

    def __init__(
        self,
        *,
        team_id: str,
        person_id: str,
        storage_settings: Any,
        roles: tuple[str, ...] = OWNER_ROLES,
        task_id: str = TASK_ID,
    ) -> None:
        self.storage_settings = storage_settings
        self.current_task_id = task_id
        self.person_id = person_id
        signed = context_signer().issue(team_id=team_id, person_id=person_id, person_roles=roles)
        # canonical_user_id locates the person's task folders (workspace scope).
        self.meta = SimpleNamespace(
            gateway_context=SimpleNamespace(task_context=signed, canonical_user_id=person_id),
            conversation_id="conversation_change",
        )

    def get_task_info(self, task_id: str) -> Any:
        return SimpleNamespace(meta=self.meta) if task_id else None

    def add_task_terminal_callback(self, _callback: Any) -> None:
        return None


def change_agent(
    config: Any,
    config_path: Path,
    *,
    authorization_enabled: bool,
    is_team_lead: bool = True,
    **extra: Any,
) -> SimpleNamespace:
    """TeamArchitect (or a CodeWritingAgent) working for the instance owner."""

    agent_type = "TeamArchitect" if is_team_lead else "CodeWritingAgent"
    team_id = config.gateway.team_id
    return SimpleNamespace(
        id="agent_team_lead" if is_team_lead else "agent_specialist",
        team_id=team_id,
        is_team_lead=is_team_lead,
        name=agent_type,
        named_agent_type=agent_type,
        access_role="worker",
        authorization_config=authorization_config(authorization_enabled),
        instance_config_path=str(config_path),
        instance_root=str(config.instance_root_path),
        bootstrap_config={"instance_root": str(config.instance_root_path)},
        workspace_dir=str(config.workspace_path),
        task_manager=SignedTaskManager(
            team_id=team_id,
            person_id=config.user.canonical_user_id,
            storage_settings=config.manager_storage_settings,
        ),
        **extra,
    )


def owner(agent: Any) -> PersonPrincipal:
    return PersonPrincipal(agent.task_manager.person_id, agent.team_id, OWNER_ROLES)


class ToolCall:
    """A Strands tool context for apply_change; its interrupt pauses until answered."""

    def __init__(self, tool_use_id: str = "tooluse_change") -> None:
        self.tool_use = {"toolUseId": tool_use_id, "name": "apply_change"}
        self.response: dict[str, Any] | None = None
        self.reasons: list[dict[str, Any]] = []

    def interrupt(self, name: str, reason: Any = None) -> Any:
        if self.response is not None:
            return self.response
        self.reasons.append(reason)
        raise InterruptException(Interrupt(id=f"interrupt-{name}", name=name, reason=reason))

    @property
    def request_id(self) -> str:
        return self.reasons[-1]["access_request_id"]


def run_apply_change(
    agent: Any, change_id: str, call: ToolCall | None = None, *, confirmed: bool = False, reason: str = "test change"
) -> dict[str, Any]:
    """Call apply_change as the agent (raises InterruptException when it pauses)."""

    def call_tool() -> str | dict[str, Any]:
        set_agent_ref(agent)
        return asyncio.run(apply_change._tool_func(change_id, reason, call or ToolCall(), confirmed=confirmed))

    return tool_json(contextvars.copy_context().run(call_tool))


def stored_request(agent: Any, call: ToolCall) -> AccessRequestV1:
    request = access_runtime.runtime_requests(agent).service.store.get_request(call.request_id)
    assert request is not None
    return request


def decide(agent: Any, call: ToolCall, choice: AccessReviewChoice = AccessReviewChoice.APPROVE) -> AccessRequestV1:
    """The owner decides the change request the paused call waits on; the call can resume."""

    decided = access_runtime.runtime_requests(agent).decide(call.request_id, owner(agent), choice)
    call.response = {"kind": "access_request_decision", "status": decided.status.value}
    return decided


def apply_with_person_approval(agent: Any, change_id: str, *, reason: str = "test change") -> tuple[dict[str, Any], str]:
    """Pause for review, let the owner approve, resume; returns the result and the request id."""

    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, change_id, call, reason=reason)
    decide(agent, call)
    return run_apply_change(agent, change_id, call, reason=reason), call.request_id


__all__ = [
    "CEDAR_BINARY",
    "SignedTaskManager",
    "TASK_ID",
    "ToolCall",
    "agreement",
    "apply_with_person_approval",
    "authorization_config",
    "change_agent",
    "decide",
    "needs_cedar",
    "owner",
    "run_apply_change",
    "stored_request",
    "use_signed_contexts",
]
