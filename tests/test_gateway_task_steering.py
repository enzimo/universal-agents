from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant_gateway.bootstrap import bootstrap_instance
from assistant_gateway.application.thread_guidance import NOT_STARTED_TEXT
from assistant_gateway.domain.request_state import RUN_STATE_RUNNING
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
    gateway.state_store.record_request(
        correlation_id="corr_running",
        session_key=session_key,
        request_kind="interactive",
        request_text="Prepare the report",
        conversation_id=session.conversation_id,
        followup_to_task_id=None,
        active_task_id=target_task_id,
        run_state=RUN_STATE_RUNNING,
    )
    return await gateway.application.send_user_message(
        text, session_key=session_key, canonical_user_id=canonical_user_id
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
async def test_user_message_during_credential_workflow_stays_with_running_request(
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
        followup_to_task_id=None,
        active_task_id="task_waiting_for_credential",
        run_state=RUN_STATE_RUNNING,
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

    assert response.message_type == "task_steering"
    assert response.text == NOT_STARTED_TEXT
    assert len(transport.operational) == 1
    assert transport.durable == []
    assert gateway.request_repository.get_held_guidance(response.correlation_id) is not None
    credential_request = gateway.state_store.get_request("corr_credential")
    assert credential_request is not None
    assert credential_request.status == credential_status


@pytest.mark.asyncio
async def test_running_request_ignores_completed_guidance(
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
        active_task_id="task_credential",
        run_state=RUN_STATE_RUNNING,
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

    running = gateway._request_service.running_request(
        session_key=session.session_key, conversation_id=session.conversation_id
    )
    assert running.correlation_id == "corr_credential_parent"
    assert running.active_task_id == "task_credential"


@pytest.mark.asyncio
async def test_guidance_for_a_task_the_lead_has_not_admitted_is_held_without_new_work(
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
        followup_to_task_id="task_previous",
        active_task_id="task_waiting_for_admission",
        run_state=RUN_STATE_RUNNING,
        status="pending",
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

    assert response.message_type == "task_steering"
    assert response.text == NOT_STARTED_TEXT
    assert len(transport.operational) == 1
    assert transport.durable == []
    running = gateway.state_store.get_request("corr_stale_active_task")
    assert running.run_state == RUN_STATE_RUNNING
    guidance = gateway.state_store.get_request(response.correlation_id)
    assert guidance.request_kind == "task_steering"
    assert guidance.status == "held"
    held = gateway.request_repository.get_held_guidance(response.correlation_id)
    assert held.target_correlation_id == running.correlation_id
    history = gateway._transcript_service.history_page(
        owner_canonical_user_id=session.owner_canonical_user_id,
        session_key=session.session_key,
        before_cursor=None,
    )
    assert [message.content for message in history.messages] == [
        "restart yourself",
        NOT_STARTED_TEXT,
    ]


@pytest.mark.asyncio
async def test_uncertain_request_publication_keeps_later_messages_on_that_request(
    tmp_path: Path,
) -> None:
    class UncertainPublication(_SteeringTransport):
        durable_attempts = 0

        async def publish_durable(self, envelope, *, routing_key):
            self.durable_attempts += 1
            # The broker may have saved the message before this error.
            raise RuntimeError("Publication acknowledgment was lost")

    transport = UncertainPublication()
    gateway = UserGateway(_config(tmp_path), manager_transport=transport)
    transport.gateway = gateway
    session = gateway.application.start_new_session()

    with pytest.raises(RuntimeError, match="acknowledgment was lost"):
        await gateway.application.send_user_message(
            "Prepare the report", session_key=session.session_key
        )
    running = gateway._request_service.running_request(
        session_key=session.session_key, conversation_id=session.conversation_id
    )
    assert running is not None
    response = await gateway.application.send_user_message(
        "Use June figures", session_key=session.session_key
    )

    assert response.message_type == "task_steering"
    assert transport.durable_attempts == 1
    steering = transport.operational[0][0].msg_content
    assert steering.target_task_id == running.active_task_id
    assert steering.target_correlation_id == running.correlation_id
