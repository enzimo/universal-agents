"""Shared setup for the ``set_up_tools`` tests (real Cedar, SQLite and TaskManager).

- ``lead_agent``: TeamLead running one task for a person, with a gateway-signed
  task context, the attributes the steering runtime needs to pause a turn,
  and ``team_comms.send_message`` recording what reaches the gateway.
- ``broker_states``: stands in for the broker's ``tool_setup.status`` answer
  (the broker action has its own tests in ``test_tool_setup_check``).
- ``gateway_result`` and ``decide``: what the gateway sends back when a page
  ends and when a person decides an access request, delivered through the
  agent's real handlers.
- ``pause_turn``: what the runtime does when a turn stops on an interrupt
  (``task_steering_runtime.send_interrupt_request_for_result``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from access_control.entities import PersonPrincipal
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from tests.task_work_support import bind_turn
from custom_agents import access_request_runtime, access_runtime, task_steering_runtime, tool_setup_runtime
from custom_agents.task_manager import TaskManager
from custom_agents.tool_setup_run_runtime import run_repository
from custom_messages.contracts.access_v2 import AccessRequestResolvedV1, AccessReviewChoice
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools import tool_setup_check
from tests.access_workflow_support import BINARY, TEAM, signed_context

GATEWAY_ROUTE = "gateway-route"
OWNER = PersonPrincipal("user-1", TEAM, ("owner",))
MEMBER = PersonPrincipal("user-2", TEAM, ("member",))


def lead_agent(
    tmp_path: Path, *, person: PersonPrincipal = OWNER, enabled: bool = True, task_id: str = "task-lead",
    instance_root: Path | None = None,
) -> SimpleNamespace:
    """TeamLead running ``task_id`` for ``person`` (see the module docstring)."""

    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity("agent-TeamArchitect", "TeamArchitect", TEAM, "TeamArchitect")
    meta = MessageMeta(conversation_id="conv-1", msg_correlation_id="corr-1")
    meta.gateway_context = GatewayContext(
        task_context=signed_context(person), session_key="s-1", canonical_user_id=person.person_id
    )
    manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(from_agent_id=GATEWAY_ROUTE), meta,
        owner_person_id=None,
    )
    return SimpleNamespace(
        id="agent-TeamArchitect",
        name="TeamArchitect",
        named_agent_type="TeamArchitect",
        team_id=TEAM,
        is_team_lead=True,
        access_role="team_lead",
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=manager,
        instance_root=str(instance_root) if instance_root else None,
        instance_config_path=None,
        test_task_id=task_id,
        _interrupted_turn_sessions={},
        _pending_interrupt_responses={},
        _pending_work_task=None,
        _should_exit=asyncio.Event(),
        paused=False,
        is_executing=False,
        main_lane_busy=False,
        _continue_pending_work=AsyncMock(),
        _access_workflow_limits_loaded=True,
        # What the steering runtime needs to record an interrupt.
        mid_turn_steering={},
        _agent_id_by_name={},
        named_agent_type_queue_map={},
        _logger=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )


def broker_states(monkeypatch: pytest.MonkeyPatch, states: dict[str, dict[str, Any]]) -> None:
    """The broker's account states per tool, as ``tool_setup.status`` returns them."""

    monkeypatch.setattr(tool_setup_check, "_broker_status", lambda tool_ids: ({key: states[key] for key in tool_ids if key in states}, ""))


def sent_pages(agent: Any) -> list[ToolSetupRequestV1]:
    """Every setup page request sent to the gateway, in order."""

    pages = []
    for call in agent.team_comms.send_message.await_args_list:
        message = call.kwargs["message"]
        assert call.kwargs["routing_key"] == GATEWAY_ROUTE
        if isinstance(message.msg_content, ToolSetupRequestV1):
            pages.append(message.msg_content)
    return pages


async def pause_turn(agent: Any, interrupt: Any, task_id: str = "task-lead") -> None:
    """The runtime pauses the turn on the tool's interrupt (and trusted code starts the run)."""

    info = agent.task_manager.get_task_info(task_id)
    bind_turn(info.task.task_id)
    await task_steering_runtime.send_interrupt_request_for_result(
        agent, result=SimpleNamespace(interrupts=[interrupt]), task=info.task, msg_header=info.header,
        msg_meta=info.meta, invoke_kwargs={}, callback_handler=lambda **_: None, full_prompt="",
    )


async def gateway_result(agent: Any, page: ToolSetupRequestV1, status: str, **extra: Any) -> None:
    """The gateway reports how a page ended, through the agent's real handler."""

    result = ToolSetupResultV1(
        setup_request_id=page.setup_request_id,
        status=status,
        parent_task_id=page.parent_task_id,
        tool_id=page.tool_id,
        kind=page.kind,
        owner=page.owner,
        step_id=page.step_id,
        **extra,
    )
    await tool_setup_runtime.handle_tool_setup_result(
        agent, result, MessageHeader(from_agent_id=GATEWAY_ROUTE), MessageMeta(msg_correlation_id="corr-1")
    )


def people_side(agent: Any) -> AccessRequests:
    service = access_runtime.runtime_access(agent).service
    return AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")


async def decide(agent: Any, request_id: str, choice: AccessReviewChoice, person: PersonPrincipal = OWNER) -> None:
    """A person decides in the Inbox; the gateway tells the agent (ids only)."""

    people_side(agent).decide(request_id, person, choice)
    await access_request_runtime.handle_access_request_resolved(
        agent, message=AccessRequestResolvedV1(message_id=f"m-{request_id}", team_id=TEAM, request_id=request_id),
        msg_header=MessageHeader(), msg_meta=MessageMeta(),
    )


def the_run(agent: Any, task_id: str = "task-lead") -> Any:
    [run] = run_repository(agent).for_task(team_id=TEAM, task_id=task_id)
    return run


def result_json(text: str) -> dict[str, Any]:
    return json.loads(text)


__all__ = [
    "GATEWAY_ROUTE",
    "MEMBER",
    "OWNER",
    "broker_states",
    "decide",
    "gateway_result",
    "lead_agent",
    "pause_turn",
    "people_side",
    "result_json",
    "sent_pages",
    "the_run",
]
