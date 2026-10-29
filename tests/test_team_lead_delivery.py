"""Exercise manager admission, beyond a successful JetStream publication."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.deferred_message_runtime import requires_execution_slot
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_transport.base import InboundDelivery


class _EmptyDeferredInbox:
    """An empty deferred inbox: the mailbox asks for messages while it has room."""

    capacity = 100

    def __init__(self) -> None:
        self.admit = AsyncMock()

    def __len__(self) -> int:
        return 0


def lead_delivery_probe(team_id="team_ua", *, is_team_lead=True, paused=False):
    """Use production routing and dispatch; stop at task handling, before any LLM."""

    @asynccontextmanager
    async def execution_scope():
        yield

    agent = SimpleNamespace(
        id="replacement_lead",
        team_id=team_id,
        named_agent_type="TeamArchitect",
        named_agent_type_queue_map={"TeamArchitect": f"{team_id}_TeamArchitect"},
        is_team_lead=is_team_lead,
        paused=paused,
        is_executing=False,
        main_lane_busy=False,
        task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime(), is_buffer_full=lambda: False),
        _logger=logging.getLogger("test.team_lead_delivery"),
        _requires_execution_slot=requires_execution_slot,
        _execution_scope=execution_scope,
        _ensure_nats_transport_adapter=lambda: SimpleNamespace(
            keepalive_while=AsyncMock()
        ),
        _handle_task_request=AsyncMock(),
        _refresh_self_agent_state=AsyncMock(),
        _continue_pending_work=AsyncMock(),
        _pending_work_task=None,
        _should_exit=asyncio.Event(),
        deferred_inbox=_EmptyDeferredInbox(),
        nats_max_deliver=3,
    )
    for method in (
        "_extract_team_id_from_routing_value",
        "_message_targets_other_team",
        "_dispatch_message",
        "on_nats_delivery",
    ):
        setattr(agent, method, MethodType(getattr(UniversalAgent, method), agent))
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False])
async def test_team_lead_accepts_own_durable_team_route(paused):
    agent = lead_delivery_probe(paused=paused)
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="human_clarify_human_msg_269992289c3f",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Prepare the exact approval request.",
        ),
        msg_header=MessageHeader(
            from_agent_id="team_ua_gateway", to_routing_path="team_ua"
        ),
        msg_meta=MessageMeta(to_named_agent_type="TeamArchitect"),
    )
    ack = AsyncMock()
    delivery = InboundDelivery(
        envelope=envelope,
        logical_route="team_ua",
        transport_subject="team_ua.lead.work",
        _ack=ack,
    )

    await agent.on_nats_delivery(delivery)

    if paused:
        agent.deferred_inbox.admit.assert_awaited_once_with(envelope)
    else:
        agent._handle_task_request.assert_awaited_once()
        assert agent._handle_task_request.call_args.kwargs["task_request"].task_id == (
            envelope.msg_content.task_id
        )
    ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_team_lead", "header_route", "logical_route"),
    [
        (True, "team_other", "team_other"),
        (True, "unrelated_recipient", "team_ua"),
    ],
)
async def test_team_route_does_not_admit_wrong_recipient(
    is_team_lead, header_route, logical_route, caplog
):
    agent = lead_delivery_probe(is_team_lead=is_team_lead)
    ack = AsyncMock()
    delivery = InboundDelivery(
        envelope=MessageEnvelope(
            msg_content=Task(
                task_id="wrong_recipient",
                task_type=MessageMetaType.REQUEST,
                problem_statement="Do not handle this task.",
            ),
            msg_header=MessageHeader(
                from_agent_id="sender", to_routing_path=header_route
            ),
        ),
        logical_route=logical_route,
        transport_subject="test.lead.work",
        _ack=ack,
    )
    await agent.on_nats_delivery(delivery)
    agent._handle_task_request.assert_not_awaited()
    agent.deferred_inbox.admit.assert_not_awaited()
    ack.assert_awaited_once()
    if header_route != "team_other":
        assert "agent_nats_delivery_target_rejected" in caplog.text
        assert "task_id=wrong_recipient" in caplog.text
