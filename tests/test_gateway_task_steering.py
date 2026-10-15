from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant_gateway.bootstrap import bootstrap_instance
from assistant_gateway.application.response_coordinator import (
    GatewayTaskSteeringRequest,
)
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import (
    UserGateway,
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
)
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
    TaskSteeringType,
)


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="steering_test",
        instance_root=str(tmp_path),
        team_id="team_steering",
        timezone="UTC",
        manager_agent_id="manager_agent_1",
        response_queue_name="team_steering_gateway",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )


async def _send_task_steering(
    gateway: UserGateway,
    text: str,
    *,
    session_key: str,
    target_task_id: str,
    canonical_user_id: str | None = None,
):
    session = gateway.session_repository.get_session(session_key)
    assert session is not None
    route = GatewayRequestRoute(canonical_user_id=canonical_user_id)
    presentation = gateway._request_context_service.requester_presentation_for_session(
        session,
        route,
    )
    return await gateway._response_coordinator.send_task_steering(
        GatewayTaskSteeringRequest(
            text=text,
            session_key=session.session_key,
            conversation_id=session.conversation_id,
            gateway_context=gateway._request_context_service.build_gateway_context(
                session_key=session.session_key,
                delivery_mode="interactive",
                route=route,
                manager_agent_id=gateway._manager_client.manager_id,
            ),
            requester_display_name=presentation.display_name,
            request_timezone=presentation.timezone,
            target_task_id=target_task_id,
        )
    )


class _SteeringTransport:
    def __init__(self) -> None:
        self.gateway: UserGateway | None = None
        self.operational: list[tuple[MessageEnvelope, str]] = []

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        del envelope, routing_key

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.operational.append((envelope, routing_key))
        assert self.gateway is not None
        steering = envelope.msg_content
        assert isinstance(steering, TaskSteering)
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=TaskSteering(
                    steering_type=TaskSteeringType.STATUS,
                    target_task_id=steering.target_task_id,
                    target_correlation_id=(steering.target_correlation_id),
                    conversation_id=steering.conversation_id,
                    status=TaskSteeringStatus.ACCEPTED,
                    status_reason="accepted",
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_agent_1",
                    to_routing_path="team_steering_gateway",
                    agent_signature="",
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id=(envelope.msg_meta.msg_correlation_id),
                    conversation_id=steering.conversation_id,
                    requester_type=RequesterType.AGENT_REQUESTER,
                ),
            )
        )


class _StaleSteeringThenTaskTransport:
    def __init__(self) -> None:
        self.gateway: UserGateway | None = None
        self.operational: list[MessageEnvelope] = []
        self.durable: list[MessageEnvelope] = []

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        del routing_key
        self.operational.append(envelope)
        assert self.gateway is not None
        steering = envelope.msg_content
        assert isinstance(steering, TaskSteering)
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=TaskSteering(
                    steering_id=steering.steering_id,
                    steering_type=TaskSteeringType.STATUS,
                    target_task_id=steering.target_task_id,
                    target_correlation_id=steering.target_correlation_id,
                    conversation_id=steering.conversation_id,
                    status=TaskSteeringStatus.STALE,
                    status_code=TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND,
                    status_reason="Target task was not found.",
                    idempotency_key=steering.idempotency_key,
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_agent_1",
                    to_routing_path="team_steering_gateway",
                    agent_signature="",
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id=envelope.msg_meta.msg_correlation_id,
                    conversation_id=steering.conversation_id,
                    requester_type=RequesterType.AGENT_REQUESTER,
                ),
            )
        )

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        del routing_key
        self.durable.append(envelope)
        assert self.gateway is not None
        task = envelope.msg_content
        assert isinstance(task, Task)
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=Task(
                    task_id=task.task_id,
                    task_type=MessageMetaType.RESPONSE,
                    answer="Restart request accepted.",
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_agent_1",
                    to_routing_path="team_steering_gateway",
                    agent_signature="",
                ),
                msg_meta=envelope.msg_meta.model_copy(
                    update={
                        "requester_type": RequesterType.USER_REQUESTER,
                        "is_response_requested": False,
                    }
                ),
            )
        )


@pytest.mark.asyncio
async def test_gateway_task_steering_uses_session_owner_display_name(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.user.display_name = "Riley"
    config.gateway.users.enabled = True
    config.save()
    bootstrap_instance(config)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    transport = _SteeringTransport()
    gateway = UserGateway(
        config,
        state_store=store,
        manager_transport=transport,
    )
    transport.gateway = gateway
    alice = store.create_gateway_user(
        display_name="Alice",
        timezone="America/Chicago",
    )
    store.create_team_membership(
        canonical_user_id=alice.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )
    session = store.create_session(
        session_key="website:web-chat:alice",
        conversation_id="conversation_alice",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=alice.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=alice.canonical_user_id,
    )

    await _send_task_steering(
        gateway,
        "include Alice's constraint",
        session_key=session.session_key,
        target_task_id="task_active",
        canonical_user_id=alice.canonical_user_id,
    )

    assert len(transport.operational) == 1
    published, _ = transport.operational[0]
    assert published.msg_meta.from_agent_name == "Alice"
    assert published.msg_meta.request_timezone == "America/Chicago"
    assert published.msg_meta.gateway_context is not None
    assert (
        published.msg_meta.gateway_context.canonical_user_id == alice.canonical_user_id
    )


@pytest.mark.parametrize(
    ("request_kind", "credential_status"),
    [
        ("interactive", REQUEST_STATUS_WAITING_FOR_CREDENTIAL),
        (
            "secure_credential_collection_result",
            REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
        ),
    ],
)
@pytest.mark.asyncio
async def test_user_message_during_credential_workflow_starts_new_conversation_task(
    tmp_path: Path,
    request_kind: str,
    credential_status: str,
) -> None:
    transport = _StaleSteeringThenTaskTransport()
    gateway = UserGateway(_config(tmp_path), manager_transport=transport)
    transport.gateway = gateway
    session = gateway.application.resolve_session(
        channel_name="cli", user_id="user", chat_id="cli"
    )
    gateway.state_store.record_request(
        correlation_id="corr_credential",
        session_key=session.session_key,
        request_kind=request_kind,
        request_text="store credential",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_waiting_for_credential",
        status=credential_status,
    )
    gateway._manager_client.register_response_channel(
        "corr_credential",
        queue=asyncio.Queue(),
    )

    try:
        response = await gateway.application.send_user_message(
            "restart yourself", session_key=session.session_key
        )
    finally:
        gateway._manager_client.unregister_response_channel("corr_credential")

    assert response.message_type == "task"
    assert response.text == "Restart request accepted."
    assert transport.operational == []
    assert len(transport.durable) == 1
    published_task = transport.durable[0].msg_content
    assert isinstance(published_task, Task)
    assert published_task.problem_statement == "restart yourself"
    credential_request = gateway.state_store.get_request("corr_credential")
    assert credential_request is not None
    assert credential_request.status == credential_status


@pytest.mark.asyncio
async def test_resolve_target_skips_completed_steering_operation(
    tmp_path: Path,
) -> None:
    gateway = UserGateway(_config(tmp_path))
    session = gateway.application.resolve_session(
        channel_name="cli",
        user_id="user",
        chat_id="cli",
    )
    gateway.state_store.record_request(
        correlation_id="corr_credential_parent",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="store credential",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_waiting_for_credential",
        status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    )
    gateway.state_store.record_request(
        correlation_id="corr_completed_steering",
        session_key=session.session_key,
        request_kind="task_steering",
        request_text="include this detail",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_waiting_for_credential",
        status="accepted",
    )

    assert await gateway._response_coordinator.resolve_target(
        session_key=session.session_key,
        conversation_id=session.conversation_id,
    ) == ("task_waiting_for_credential", "corr_credential_parent")


@pytest.mark.asyncio
async def test_stale_active_task_steering_is_readmitted_as_new_web_request(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.save()
    bootstrap_instance(config)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    transport = _StaleSteeringThenTaskTransport()
    gateway = UserGateway(
        config,
        state_store=store,
        manager_transport=transport,
    )
    transport.gateway = gateway
    user = store.create_gateway_user(
        display_name="Web User",
        timezone="UTC",
    )
    store.create_team_membership(
        canonical_user_id=user.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )
    session = store.create_session(
        session_key="website:web-chat:stale-steering",
        conversation_id="conversation_stale_steering",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=user.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="corr_stale_active_task",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="Long-running request",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_already_archived",
        status="waiting_for_steering",
    )

    response = await gateway.application.send_user_message(
        "restart yourself",
        session_key=session.session_key,
        channel_name="website",
        user_id=user.canonical_user_id,
        chat_id="web-chat",
        original_message_id="web_restart_request",
        canonical_user_id=user.canonical_user_id,
    )

    assert response.message_type == "task"
    assert response.text == "Restart request accepted."
    assert len(transport.operational) == 1
    assert len(transport.durable) == 1
    published_task = transport.durable[0].msg_content
    assert isinstance(published_task, Task)
    assert published_task.problem_statement == "restart yourself"
    stale_request = gateway.state_store.get_request("corr_stale_active_task")
    assert stale_request is not None
    assert stale_request.status == "stale"
    recovered_request = gateway.state_store.get_request(response.correlation_id)
    assert recovered_request is not None
    assert recovered_request.request_kind == "interactive"
    assert recovered_request.status == "completed"
    history = gateway._transcript_service.history_page(
        owner_canonical_user_id=session.owner_canonical_user_id,
        session_key=session.session_key,
        before_cursor=None,
    )
    assert [message.content for message in history.messages] == [
        "restart yourself",
        "Restart request accepted.",
    ]
