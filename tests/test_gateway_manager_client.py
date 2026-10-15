from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerClientSettings,
)
from custom_messages.contracts.control_v1 import AgentStatus, ManagerMemoryCommand, PinnedMemoryCommandPayload, SystemStatusMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


class _FixedIds:
    def new_correlation_id(self) -> str:
        return "corr_id_manager"

    def new_local_correlation_id(self) -> str:
        return "local_corr_manager"

    def new_scheduled_job_id(self) -> str:
        return "cron_manager"

    def new_session_key(self) -> str:
        return "session_manager"

    def new_conversation_id(self) -> str:
        return "conversation_id_manager"


class _FakeManagerRuntime:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.on_response = None
        self.on_operational = None
        self.on_presence = None
        self.durable: list[tuple[MessageEnvelope, str]] = []
        self.operational: list[tuple[MessageEnvelope, str]] = []
        self.consumer_result: bool | None = True
        self.consumer_probes: list[str] = []
        self.put_calls: list[dict[str, object]] = []
        self.operational_publish_hook = None

    async def connect(self, *, on_response, on_operational, on_presence) -> None:
        self.connected = True
        self.on_response = on_response
        self.on_operational = on_operational
        self.on_presence = on_presence

    async def close(self) -> None:
        self.connected = False
        self.closed = True

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
        self.operational.append((envelope, routing_key))
        if self.operational_publish_hook is not None:
            await self.operational_publish_hook(envelope)

    def stopped_receivers(self) -> tuple:
        return ()

    async def manager_has_live_consumer(
        self,
        manager_id: str,
    ) -> bool | None:
        self.consumer_probes.append(manager_id)
        return self.consumer_result

    async def put_object(self, **kwargs) -> object:
        self.put_calls.append(dict(kwargs))
        return object()


def _settings(*, manager_id: str | None = "manager_1") -> GatewayManagerClientSettings:
    return GatewayManagerClientSettings(
        instance_name="test_instance",
        user_display_name="Test User",
        team_id="team_test",
        manager_named_agent_type="TeamArchitect",
        response_route="team_test_gateway_response",
        initial_manager_id=manager_id,
        liveness_poll_seconds=0.01,
        presence_stale_seconds=30.0,
        recovery_discovery_timeout_seconds=0.01,
    )


def _system_envelope(correlation_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=SystemStatusMessage(system_status="ok"),
        msg_header=MessageHeader(from_agent_id="manager_1"),
        msg_meta=MessageMeta(msg_correlation_id=correlation_id),
    )


def _memory_response(correlation_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=ManagerMemoryCommand(
            command_id="manager_memory_response",
            command_type=MessageMetaType.RESPONSE,
            command=PinnedMemoryCommandPayload(
                bank="usermem",
                action="list",
            ),
            success=True,
            rendered_result="No memories found.",
        ),
        msg_header=MessageHeader(from_agent_id="manager_1"),
        msg_meta=MessageMeta(
            msg_correlation_id=correlation_id,
            conversation_id="conversation_memory",
        ),
    )


def _client(
    runtime: _FakeManagerRuntime,
    *,
    settings: GatewayManagerClientSettings | None = None,
    unsolicited: list[MessageEnvelope] | None = None,
    route_writes: list[str | None] | None = None,
    available: list | None = None,
    unavailable: list | None = None,
    clock: _FixedClock | None = None,
    presence: list[tuple[AgentStatus, datetime | None, str]] | None = None,
) -> GatewayManagerClient:
    async def on_unsolicited(envelope: MessageEnvelope) -> None:
        if unsolicited is not None:
            unsolicited.append(envelope)

    return GatewayManagerClient(
        settings=settings or _settings(),
        clock=clock or _FixedClock(
            datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        ),
        id_generator=_FixedIds(),
        logger=logging.getLogger("test.gateway.manager_client"),
        runtime=runtime,
        on_unsolicited=on_unsolicited,
        on_agent_presence=(
            (lambda status, timestamp, source: presence.append((status, timestamp, source)))
            if presence is not None
            else None
        ),
        write_manager_route=(
            (lambda manager_id: route_writes.append(manager_id))
            if route_writes is not None
            else None
        ),
        on_manager_available=(
            (lambda transition: available.append(transition))
            if available is not None
            else None
        ),
        on_manager_unavailable=(
            (lambda transition: unavailable.append(transition))
            if unavailable is not None
            else None
        ),
    )


@pytest.mark.asyncio
async def test_manager_client_drops_late_memory_control_response(
    caplog,
) -> None:
    unsolicited: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)

    with caplog.at_level(
        logging.WARNING,
        logger="test.gateway.manager_client",
    ):
        await client.handle_response(_memory_response("corr_memory_expired"))

    assert unsolicited == []
    assert "event=gateway_unmatched_manager_control_response_dropped" in caplog.text


@pytest.mark.asyncio
async def test_manager_client_rejects_duplicate_correlation_registration() -> None:
    client = _client(_FakeManagerRuntime())
    client.register_response_channel("corr_duplicate")

    with pytest.raises(RuntimeError, match="already registered"):
        client.register_response_channel("corr_duplicate")


@pytest.mark.asyncio
async def test_pending_response_ids_follow_the_open_reply_waits() -> None:
    client = _client(_FakeManagerRuntime())
    async with client.response_channel("corr_waiting"):
        assert client.pending_response_correlation_ids() == ("corr_waiting",)
    assert client.pending_response_correlation_ids() == ()


@pytest.mark.asyncio
async def test_runtime_backed_publish_requires_explicit_connection() -> None:
    runtime = _FakeManagerRuntime()
    client = _client(runtime)

    with pytest.raises(RuntimeError, match="Gateway is not connected"):
        await client.publish_durable(
            _system_envelope("corr_not_connected"),
            routing_key="manager_1",
        )

    assert runtime.durable == []


@pytest.mark.asyncio
async def test_manager_client_presence_timeout_is_clock_deterministic() -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    runtime = _FakeManagerRuntime()
    runtime.consumer_result = None
    route_writes: list[str | None] = []
    client = _client(
        runtime,
        route_writes=route_writes,
        clock=_FixedClock(now),
    )
    client.manager_status_updated_at = now - timedelta(seconds=31)

    await client.check_manager_liveness_once()

    assert route_writes == [None]
    assert client.manager_id is None


@pytest.mark.asyncio
async def test_manager_client_connects_before_object_transfer_and_closes() -> None:
    runtime = _FakeManagerRuntime()
    client = _client(runtime)

    await client.put_object(
        bucket="attachments",
        object_name="att/file.txt",
        path="/tmp/file.txt",
        ttl_seconds=60,
        max_bytes=1024,
        headers={"content-type": "text/plain"},
    )
    await client.close()

    assert runtime.put_calls == [
        {
            "bucket": "attachments",
            "object_name": "att/file.txt",
            "path": "/tmp/file.txt",
            "ttl_seconds": 60,
            "max_bytes": 1024,
            "headers": {"content-type": "text/plain"},
        }
    ]
    assert runtime.closed is True


def _knowledge_response(correlation_id: str) -> MessageEnvelope:
    from custom_messages.contracts.control_v1 import (
        KnowledgeCommandAction,
        KnowledgeCommandPayload,
        KnowledgeCommandResultPayload,
        ManagerKnowledgeCommand,
    )

    return MessageEnvelope(
        msg_content=ManagerKnowledgeCommand(
            command_id="mgr_knowledge_response",
            command_type=MessageMetaType.RESPONSE,
            command=KnowledgeCommandPayload(action=KnowledgeCommandAction.LIST),
            success=True,
            result=KnowledgeCommandResultPayload(action=KnowledgeCommandAction.LIST),
        ),
        msg_header=MessageHeader(from_agent_id="manager_1"),
        msg_meta=MessageMeta(msg_correlation_id=correlation_id),
    )


@pytest.mark.asyncio
async def test_knowledge_page_replies_reach_their_waiting_request_and_late_ones_are_dropped(caplog) -> None:
    """The Knowledge page's command is a gateway manager control (one list drives routing and reply matching)."""

    from custom_messages.contracts.control_v1 import ManagerKnowledgeCommand
    from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES

    unsolicited: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)

    async with client.control_channel("corr_knowledge") as replies:
        await client.handle_response(_knowledge_response("corr_knowledge"))
        delivered = replies.get_nowait()
    with caplog.at_level(logging.WARNING, logger="test.gateway.manager_client"):
        await client.handle_response(_knowledge_response("corr_knowledge_late"))

    assert ManagerKnowledgeCommand in GATEWAY_MANAGER_CONTROL_TYPES
    assert delivered.msg_content.command_id == "mgr_knowledge_response"
    assert unsolicited == []
    assert "event=gateway_unmatched_manager_control_response_dropped" in caplog.text
