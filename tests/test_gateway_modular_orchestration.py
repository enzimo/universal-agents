from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

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
