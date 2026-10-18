from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import AsyncMock

from assistant_gateway.application.manager_commands import GatewayManagerCommandRequest
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_DELETE,
    RESOURCE_SCHEDULE,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from assistant_gateway.application.request_context import GatewayCommandPerson
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ManagerScheduleCommand


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="modular_gateway_test",
        instance_root=str(tmp_path),
        team_id="team_modular",
        timezone="UTC",
        manager_agent_id="manager_1",
        response_queue_name="gateway_queue",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )


class _BlockingSecureBroker:
    available = True
    collection_ttl_seconds = 900

    def user_form_url(self, internal_url: str) -> str:
        return internal_url.replace("internal", "public")

    async def create_collection_session(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        assert request["credential_key"] == "test_token"
        assert context["canonical_user_id"] == "user_1"
        return {
            "session_id": "broker_session_1",
            "form_url": "http://internal/form/broker_session_1",
            "status_url": "http://internal/status/broker_session_1",
            "expires_at": "2099-08-29T04:06:47+00:00",
        }

    async def execute_approved_signup(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        del request, context
        return None

    async def get_collection_status(
        self,
        *,
        status_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        del status_url, session_id
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_attempt", ["sent", "unavailable", "transport_error", "closing_error", "broker_missing", "save_failure"])
async def test_credential_form_completion_is_recovered_after_gateway_restart(tmp_path, monkeypatch, first_attempt):
    config = _config(tmp_path)
    broker = _BlockingSecureBroker()
    store_path = str(tmp_path / "credential_recovery.db")
    gateway = UserGateway(config, state_store=GatewayStateStore(store_path), secure_broker=broker)
    gateway.state_store.create_session(
        session_key="session_1", conversation_id="conversation_1", manager_agent_id="manager_1",
        owner_canonical_user_id="user_1",
    )
    gateway.state_store.record_request(
        correlation_id="correlation_1", session_key="session_1", request_kind="interactive",
        request_text="store credential", conversation_id="conversation_1", followup_to_task_id=None,
    )
    request = SecureCredentialCollectionRequest(
        credential_request_id="form_before_restart", parent_task_id="task_1", service_name="Example",
        credential_key="test_token", purpose="Store the service token.", continue_task=True,
    )
    if first_attempt == "save_failure":
        def fail_to_save(_record):
            raise RuntimeError("Write failed.")

        monkeypatch.setattr(gateway._credential_workflow._collections, "record_credential_collection", fail_to_save)
        gateway._manager_client.publish_durable = AsyncMock()
    response = await gateway._credential_workflow.start_collection(
        request_content=request, request_envelope=MessageEnvelope(msg_content=request),
        correlation_id="correlation_1", conversation_id="conversation_1", session_key="session_1",
    )
    if first_attempt == "save_failure":
        assert response.transient_action is None
        assert gateway._credential_workflow.pending_poll_count == 0
        assert gateway.state_store.credential_collection_repository.list_credential_collections() == []
        await gateway.close()
        return
    await gateway.close()

    broker.get_collection_status = AsyncMock(return_value={
        "session_id": "broker_session_1", "status": "credential_stored",
        "result": {"content": {"status": "credential_stored", "message": "Stored."}},
    })
    if first_attempt == "broker_missing":
        from assistant_gateway.application.ports import GatewaySecureBrokerRefusal

        broker.get_collection_status.side_effect = GatewaySecureBrokerRefusal("The form session is gone.", status=404)
    restarted = UserGateway(config, state_store=GatewayStateStore(store_path), secure_broker=broker)
    restarted._credential_workflow._settings = replace(
        restarted._credential_workflow._settings, poll_interval_seconds=0.001,
    )
    publish = AsyncMock()
    restarted._manager_client.publish_durable = publish
    if first_attempt in {"unavailable", "transport_error"}:
        original_publish = restarted._credential_workflow._publisher.publish
        calls = 0

        async def refuse_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                if first_attempt == "transport_error":
                    raise RuntimeError("The transport disconnected.")
                return False
            return await original_publish(*args, **kwargs)

        restarted._credential_workflow._publisher.publish = refuse_once
    if first_attempt == "closing_error":
        monkeypatch.setattr(restarted._credential_workflow, "_post_closing", AsyncMock(side_effect=RuntimeError("Chat output unavailable.")))
    monkeypatch.setattr(restarted._manager_client, "connect", AsyncMock())
    monkeypatch.setattr(restarted._manager_client, "refresh_manager_id", AsyncMock())
    monkeypatch.setattr(restarted._maintenance_schedules, "ensure_all", AsyncMock())
    try:
        await restarted.connect()
        for _ in range(150):
            if publish.await_count:
                break
            await asyncio.sleep(0.01)
        from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionResult

        results = [call.args[0].msg_content for call in publish.await_args_list if isinstance(call.args[0].msg_content, SecureCredentialCollectionResult)]
        assert len(results) == 1
        assert results[0].credential_request_id == request.credential_request_id
        assert results[0].status == ("credential_unavailable" if first_attempt == "broker_missing" else "credential_stored")
        assert results[0].continue_task is True
        if first_attempt in {"unavailable", "transport_error"}:
            assert calls == 2
        broker.get_collection_status.assert_awaited_once_with(status_url="", session_id="broker_session_1")
        assert restarted.state_store.credential_collection_repository.list_credential_collections() == []
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_gateway_shutdown_cancels_owned_credential_poll_tasks(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    gateway = UserGateway(
        config,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        secure_broker=_BlockingSecureBroker(),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conversation_1",
        manager_agent_id="manager_1",
        owner_canonical_user_id="user_1",
    )
    gateway.state_store.record_request(
        correlation_id="correlation_1",
        session_key="session_1",
        request_kind="interactive",
        request_text="store credential",
        conversation_id="conversation_1",
        followup_to_task_id=None,
    )
    request = SecureCredentialCollectionRequest(
        service_name="Test",
        credential_key="test_token",
        purpose="Test lifecycle ownership.",
        parent_task_id="task_1",
    )

    response = await gateway._credential_workflow.start_collection(
        request_content=request,
        request_envelope=MessageEnvelope(
            msg_content=request,
            msg_header=MessageHeader(
                from_agent_id="manager_1",
                to_routing_path="gateway_queue",
                agent_signature="",
            ),
            msg_meta=MessageMeta(
                msg_correlation_id="correlation_1",
                conversation_id="conversation_1",
            ),
        ),
        correlation_id="correlation_1",
        conversation_id="conversation_1",
        session_key="session_1",
    )

    assert "http://public/form/broker_session_1" in response.text
    assert gateway._credential_workflow.pending_poll_count == 1
    await gateway._credential_workflow.close()
    assert gateway._credential_workflow.pending_poll_count == 0


class _DenyAuthorizer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def can_access(self, **kwargs: Any) -> bool:
        self.requests.append(kwargs)
        return False


class _CaptureTransport:
    def __init__(self) -> None:
        self.durable: list[tuple[MessageEnvelope, str]] = []

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.durable.append((envelope, routing_key))

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        del envelope, routing_key


@pytest.mark.asyncio
async def test_manager_command_authorization_denies_before_publication(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    authorizer = _DenyAuthorizer()
    transport = _CaptureTransport()
    gateway = UserGateway(
        config,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
        private_authorizer=authorizer,
    )
    session = gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conversation_1",
        manager_agent_id="manager_1",
        owner_canonical_user_id="user_1",
    )
    command = ManagerScheduleCommand(
        action="remove",
        schedule_id="schedule_1",
    )

    with pytest.raises(PermissionError, match="Private resource"):
        await gateway._manager_commands.request(
            GatewayManagerCommandRequest(
                command=command,
                session=session,
                person=GatewayCommandPerson(
                    canonical_user_id="user_1", team_id="team_modular", roles=("member",)
                ),
                route=GatewayRequestRoute(
                    channel_name="telegram",
                    user_id="external_1",
                    chat_id="chat_1",
                ),
            )
        )

    assert transport.durable == []
    assert authorizer.requests == [
        {
            "actor_canonical_user_id": "user_1",
            "owner_canonical_user_id": "user_1",
            "resource_type": RESOURCE_SCHEDULE,
            "resource_id": "schedule_1",
            "action": PRIVATE_ACTION_DELETE,
            "team_id": "team_modular",
            "roles": ("member",),
        }
    ]
