"""Late and repeated questions through real JetStream and gateway persistence."""

from __future__ import annotations

import asyncio

import pytest

from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task, TaskAnswer, TaskQuery, TaskStatus
from custom_messages.enums import MessageMetaType, RequesterType, TaskQueryReason
from custom_transport.base import SubscriptionDelivery
from id_generation import new_id
from live_nats_client import LiveNatsClient, resolve_live_transport_settings


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
async def test_late_and_repeated_pause_replies_over_nats(tmp_path):
    team_id = new_id("query_test")
    config = GatewayInstanceConfig.create(
        instance_name=team_id,
        instance_root=str(tmp_path / "instance"),
        team_id=team_id,
        timezone="UTC",
        manager_agent_id=new_id("manager"),
    )
    config.transport = resolve_live_transport_settings()
    config.delivery.quiet_hours.enabled = False
    delivered = asyncio.Queue()

    def make_gateway():
        return UserGateway(
            config,
            output_sink=None,
            event_sink=delivered.put_nowait,
            state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        )

    gateway = make_gateway()
    session = gateway.application.start_new_session()
    correlation = new_id("correlation")
    parent = new_id("task")
    meta = MessageMeta(
        msg_correlation_id=correlation,
        conversation_id=session.conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(
            session_key=session.session_key,
            delivery_mode="interactive",
            canonical_user_id=config.user.canonical_user_id,
        ),
    )

    def envelope(content, *, response_meta=meta):
        return MessageEnvelope(
            msg_content=content,
            msg_meta=response_meta,
            msg_header=MessageHeader(
                from_agent_id=config.manager.manager_agent_id,
                to_routing_path=config.manager.response_queue_name,
            ),
        )

    async with LiveNatsClient(team_id, transport=config.transport) as manager:
        replies = await manager.subscribe_route(config.manager.manager_agent_id)

        async def answer_status(delivery):
            request = delivery.envelope.msg_content
            if (
                not isinstance(request, TaskStatus)
                or request.status_type != MessageMetaType.REQUEST
            ):
                return
            await manager.publish(
                config.manager.response_queue_name,
                envelope(
                    request.model_copy(
                        update={
                            "status_type": MessageMetaType.RESPONSE,
                            "is_known_task": True,
                        }
                    ),
                    response_meta=delivery.envelope.msg_meta,
                ),
            )

        await manager.adapter.subscribe_subject(
            name="pause-parent-status",
            subject=manager.mapper.operational_subject(),
            handler=answer_status,
            logical_route="status",
            delivery=SubscriptionDelivery.CORE,
        )
        await gateway._manager_client.connect()
        try:
            query = TaskQuery(
                query_type=MessageMetaType.REQUEST,
                parent_task_id=parent,
                message="Budget reached. Reply 1 to continue.",
                query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
            )
            # Deliver without a waiting HTTP/channel request, then restart the gateway.
            await manager.publish(config.manager.response_queue_name, envelope(query))
            await asyncio.wait_for(delivered.get(), timeout=10)
            await gateway._manager_client.close()
            gateway = make_gateway()
            await gateway._manager_client.connect()

            for index in range(2):
                replied_message = RepliedMessage(author="assistant", text=f"Earlier scheduled report {index}")
                [pending] = gateway.state_store.list_open_pending_queries(session.session_key)
                assert pending.task_query_id == query.task_query_id
                assert pending.was_shown
                answer = asyncio.create_task(
                    ChannelBridge(gateway.application).handle_inbound(
                        ChannelInboundMessage(
                            channel_name="telegram",
                            user_id="test-user",
                            chat_id="test-chat",
                            session_key=session.session_key,
                            text="1",
                            replied_message=replied_message,
                        )
                    )
                )
                try:
                    reply = await replies.get(timeout=10)
                    assert isinstance(reply.msg_content, TaskQuery)
                    assert reply.msg_content.query_type == MessageMetaType.RESPONSE
                    assert reply.msg_content.task_query_id == query.task_query_id
                    assert reply.msg_content.parent_task_id == parent
                    assert reply.msg_content.message == "1"
                    assert reply.msg_content.replied_message == replied_message
                    assert reply.msg_meta.msg_correlation_id == correlation
                    assert reply.msg_meta.conversation_id == session.conversation_id
                    if index == 0:
                        query = query.model_copy(
                            update={"task_query_id": new_id("query")}
                        )
                        response = query
                    else:
                        response = Task(
                            task_id=parent,
                            task_type=MessageMetaType.RESPONSE,
                            answer=TaskAnswer(answer="Finished"),
                        )
                    await manager.publish(
                        config.manager.response_queue_name, envelope(response)
                    )
                    result = await asyncio.wait_for(answer, timeout=10)
                    assert result[0].message_type == (
                        "task_query" if index == 0 else "task"
                    )
                finally:
                    if not answer.done():
                        answer.cancel()
                        await asyncio.gather(answer, return_exceptions=True)
            assert gateway.state_store.list_open_pending_queries(session.session_key) == []
        finally:
            await gateway._manager_client.close()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.parametrize("shared_correlation", [False, True])
async def test_two_questions_in_one_chat_are_answered_in_order_over_nats(tmp_path, shared_correlation):
    """Two requests ask in one chat; a restart keeps both; each answer reaches its own."""

    from assistant_gateway.application.pending_questions import QUESTION_WAITS_IN_LINE_TEXT

    team_id = new_id("query_test")
    config = GatewayInstanceConfig.create(
        instance_name=team_id,
        instance_root=str(tmp_path / "instance"),
        team_id=team_id,
        timezone="UTC",
        manager_agent_id=new_id("manager"),
    )
    config.transport = resolve_live_transport_settings()
    config.delivery.quiet_hours.enabled = False
    delivered = asyncio.Queue()

    def make_gateway():
        return UserGateway(
            config,
            output_sink=None,
            event_sink=delivered.put_nowait,
            state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        )

    gateway = make_gateway()
    session = gateway.application.start_new_session()

    def meta(correlation_id):
        return MessageMeta(
            msg_correlation_id=correlation_id,
            conversation_id=session.conversation_id,
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key=session.session_key,
                delivery_mode="interactive",
                canonical_user_id=config.user.canonical_user_id,
            ),
        )

    def envelope(content, response_meta):
        return MessageEnvelope(
            msg_content=content,
            msg_meta=response_meta,
            msg_header=MessageHeader(
                from_agent_id=config.manager.manager_agent_id,
                to_routing_path=config.manager.response_queue_name,
            ),
        )

    report = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id=new_id("task"),
        message="Which folder holds the report?",
    )
    trip = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id=new_id("task"),
        message="Which week should the trip be?",
    )
    first_correlation = new_id("correlation")
    asks = {
        report.task_query_id: (first_correlation, report),
        trip.task_query_id: (first_correlation if shared_correlation else new_id("correlation"), trip),
    }

    async with LiveNatsClient(team_id, transport=config.transport) as manager:
        replies = await manager.subscribe_route(config.manager.manager_agent_id)

        async def answer_status(delivery):
            request = delivery.envelope.msg_content
            if not isinstance(request, TaskStatus) or request.status_type != MessageMetaType.REQUEST:
                return
            await manager.publish(
                config.manager.response_queue_name,
                envelope(
                    request.model_copy(
                        update={"status_type": MessageMetaType.RESPONSE, "is_known_task": True}
                    ),
                    delivery.envelope.msg_meta,
                ),
            )

        await manager.adapter.subscribe_subject(
            name="queue-parent-status",
            subject=manager.mapper.operational_subject(),
            handler=answer_status,
            logical_route="status",
            delivery=SubscriptionDelivery.CORE,
        )
        await gateway._manager_client.connect()
        try:
            for correlation_id, question in asks.values():
                # Both requests' response waits already ended.
                await manager.publish(
                    config.manager.response_queue_name, envelope(question, meta(correlation_id))
                )
            shown = [(await asyncio.wait_for(delivered.get(), timeout=10)).text for _ in range(2)]
            assert shown == [report.message, QUESTION_WAITS_IN_LINE_TEXT]

            await gateway._manager_client.close()
            gateway = make_gateway()
            await gateway._manager_client.connect()

            waiting = []
            try:
                for text, expected in (("The shared reports folder", report), ("The second week of June", trip)):
                    answer = asyncio.create_task(
                        ChannelBridge(gateway.application).handle_inbound(
                            ChannelInboundMessage(
                                channel_name="telegram",
                                user_id="100200300",
                                chat_id="100200300",
                                session_key=session.session_key,
                                text=text,
                            )
                        )
                    )
                    waiting.append((answer, expected, text))
                    reply = await replies.get(timeout=10)
                    correlation_id, _ = asks[expected.task_query_id]
                    assert reply.msg_content.query_type == MessageMetaType.RESPONSE
                    assert reply.msg_content.task_query_id == expected.task_query_id
                    assert reply.msg_content.message == text
                    assert reply.msg_meta.msg_correlation_id == correlation_id
                    if expected is report:
                        # The next question is shown once the first is answered.
                        next_shown = await asyncio.wait_for(delivered.get(), timeout=10)
                        assert next_shown.text == trip.message
                # The tasks finish in the opposite order to the questions.
                # A shared correlation must not give either answer the
                # sibling task's final reply.
                for answer, expected, text in reversed(waiting):
                    correlation_id, _ = asks[expected.task_query_id]
                    await manager.publish(
                        config.manager.response_queue_name,
                        envelope(
                            Task(
                                task_id=expected.parent_task_id,
                                task_type=MessageMetaType.RESPONSE,
                                answer=TaskAnswer(answer=f"Done: {text}"),
                            ),
                            meta(correlation_id),
                        ),
                    )
                    result = await asyncio.wait_for(answer, timeout=10)
                    assert result[0].text == f"Done: {text}"
                    if expected is trip:
                        assert not waiting[0][0].done()
            finally:
                for answer, _, _ in waiting:
                    answer.cancel()
                await asyncio.gather(*(answer for answer, _, _ in waiting), return_exceptions=True)
            assert gateway.state_store.list_open_pending_queries(session.session_key) == []
        finally:
            await gateway._manager_client.close()
