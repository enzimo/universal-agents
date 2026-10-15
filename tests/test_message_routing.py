import logging

import pytest

from custom_agents.message_routing import (
    OutgoingRoutingError,
    build_outgoing_meta,
    resolve_outgoing_routing,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RoutingType


@pytest.mark.asyncio
async def test_build_outgoing_meta_debug_logs_when_preserving_gateway_context_without_llm_meta(
    caplog,
):
    incoming_meta = MessageMeta(
        msg_correlation_id="corr_warn",
        conversation_id="conversation_id_warn",
        gateway_context=GatewayContext(
            session_key="session_warn",
            chat_id="chat_warn",
        ),
    )
    content = Task(
        task_id="task_warn",
        task_type=MessageMetaType.RESPONSE,
        answer="ok",
    )

    with caplog.at_level("DEBUG"):
        outgoing_meta = await build_outgoing_meta(
            llm_meta=None,
            incoming_meta=incoming_meta,
            content=content,
            from_agent_name="manager",
        )

    assert outgoing_meta.gateway_context is not None
    assert "omitted gateway_context" in caplog.text
    assert "WARNING" not in caplog.text


@pytest.mark.asyncio
async def test_response_without_requester_route_fails_before_header_is_mutated(caplog):
    response = Task(
        task_id="task_unroutable",
        task_type=MessageMetaType.RESPONSE,
        answer="done",
    )
    header = MessageHeader(
        from_agent_id="manager",
        to_routing_path=RoutingType.NULL_ROUTE.value,
        response_routing_key="manager_response_route",
    )

    with (
        caplog.at_level("ERROR"),
        pytest.raises(
            OutgoingRoutingError,
            match="no resolvable requester route",
        ),
    ):
        await resolve_outgoing_routing(
            content=response,
            header=header,
            incoming_header=MessageHeader(
                from_agent_id=None,
                response_routing_key=None,
                to_routing_path=RoutingType.NULL_ROUTE.value,
            ),
            incoming_meta=MessageMeta(
                msg_correlation_id="corr_unroutable",
                conversation_id="conversation_unroutable",
            ),
            agent_id_by_name={},
            named_agent_type_queue_map={},
            team_id="team_1",
            logger=logging.getLogger("test_message_routing"),
            agent_id="manager",
        )

    assert header.to_routing_path == RoutingType.NULL_ROUTE.value
    assert header.response_routing_key == "manager_response_route"
    assert "event=outgoing_response_route_unresolved" in caplog.text
    assert "corr_unroutable" in caplog.text


@pytest.mark.parametrize("response_route,sender,expected", [
    ("gateway_reply", "originator", "gateway_reply"),
    (None, "originator", "originator"),
    (None, None, None),
])
def test_tool_setup_primary_route_uses_trusted_header(response_route, sender, expected):
    from custom_agents.message_routing import requester_bound_primary_route
    from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1
    request = ToolSetupRequestV1(
        parent_task_id="task", tool_id="cli:x", kind="sign_in", owner="person", reason="Post to X",
    )
    assert requester_bound_primary_route(request, MessageHeader(
        response_routing_key=response_route, from_agent_id=sender,
    )) == expected
