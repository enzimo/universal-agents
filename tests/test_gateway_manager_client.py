from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerClientSettings,
)
from custom_messages.contracts.control_v1 import AgentStatus, ManagerRuntimeWorkCommand, SystemStatusMessage
from custom_messages.contracts.task_v1 import Task
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
        self.durable: list[tuple[MessageEnvelope, str]] = []
        self.operational: list[tuple[MessageEnvelope, str]] = []
        self.put_calls: list[dict[str, object]] = []
        self.operational_publish_hook = None

    async def connect(self, *, on_response, on_operational) -> None:
        self.connected = True
        self.on_response = on_response
        self.on_operational = on_operational

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

    async def jetstream_usage(self) -> None:
        return None

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
    )


def _system_envelope(correlation_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=SystemStatusMessage(system_status="ok"),
        msg_header=MessageHeader(from_agent_id="manager_1"),
        msg_meta=MessageMeta(msg_correlation_id=correlation_id),
    )


def _late_control_response(correlation_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=ManagerRuntimeWorkCommand(
            command_id="manager_runtime_response",
            command_type=MessageMetaType.RESPONSE,
            action="cancel",
            scope="user",
            success=True,
            message="cancelled",
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
async def test_manager_client_drops_late_manager_control_response(
    caplog,
) -> None:
    unsolicited: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)

    with caplog.at_level(
        logging.WARNING,
        logger="test.gateway.manager_client",
    ):
        await client.handle_response(_late_control_response("corr_control_expired"))

    assert unsolicited == []
    assert "event=gateway_unmatched_manager_control_response_dropped" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", [None, "task_one"])
async def test_manager_client_rejects_duplicate_correlation_registration(task_id) -> None:
    client = _client(_FakeManagerRuntime())
    client.register_response_channel("corr_duplicate", task_id=task_id)

    with pytest.raises(RuntimeError, match="already registered"):
        client.register_response_channel("corr_duplicate", task_id=task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["response", "operational"])
async def test_task_reply_waits_share_a_correlation_without_consuming_sibling_replies(channel) -> None:
    unsolicited = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)
    receive = getattr(client, f"handle_{channel}")

    def reply(task_id):
        return MessageEnvelope(
            msg_content=Task(task_id=task_id, task_type=MessageMetaType.RESPONSE, answer="Done"),
            msg_header=MessageHeader(from_agent_id="manager_1"),
            msg_meta=MessageMeta(msg_correlation_id="corr_shared"),
        )

    async with client.response_channel("corr_shared", task_id="task_one") as first:
        async with client.response_channel("corr_shared", task_id="task_two") as second:
            assert client.pending_response_ids == ("corr_shared",)
            await receive(reply("task_two"))
            assert (await second.get()).msg_content.task_id == "task_two"
            assert first.empty()
            await receive(reply("task_other"))
            assert first.empty() and second.empty()
            assert len(unsolicited) == (1 if channel == "response" else 0)
            # A service-wide availability notice must release every wait,
            # even though only one correlation is exposed to the watcher.
            await client.queue_response("corr_shared", _system_envelope("corr_shared"))
            assert (await first.get()).msg_content.system_status == "ok"
            assert (await second.get()).msg_content.system_status == "ok"
        assert client.has_pending_response("corr_shared")
    assert client.pending_response_ids == ()


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
async def test_the_lead_from_the_records_sets_and_clears_the_route() -> None:
    """The team view applies the lead's records (assistant_gateway.team_view); a leaving lead clears the route."""

    route_writes: list[str | None] = []
    available: list = []
    unavailable: list = []
    client = _client(_FakeManagerRuntime(), settings=_settings(manager_id=None), route_writes=route_writes,
                     available=available, unavailable=unavailable)

    def lead(agent_id: str, *, exiting: bool = False) -> AgentStatus:
        return AgentStatus(self_id=agent_id, team_id="team_test", name="TeamLead", named_agent_type="TeamArchitect",
                           agent_role="team_lead", is_paused=False, is_exiting=exiting,
                           status_type=MessageMetaType.RESPONSE,
                           last_seen_at=datetime.now(timezone.utc).isoformat())

    await client.apply_manager_status(lead("lead_1"), source="agent_records")
    await client.apply_manager_status(lead("lead_1", exiting=True), source="agent_records")

    assert route_writes == ["lead_1", None] and client.manager_id is None
    assert [item.manager_id for item in available] == ["lead_1"]
    assert [item.manager_id for item in unavailable] == ["lead_1"]


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
