from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerClientSettings,
)
from assistant_gateway.team_view import GatewayTeamView
from custom_messages.contracts.control_v1 import AgentStatus, ManagerRuntimeWorkCommand, SystemStatusMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType
from custom_messages.wire.encoding import format_timestamp
from custom_transport.protocol1.host_runtime import LIVENESS_INTERVAL, LIVENESS_LIFETIME, MessageHost
from custom_transport.protocol1.team_agents import RECHECK_SECONDS, TeamAgents, _Held

LEAD_HOST = "lead-host"


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
@pytest.mark.parametrize("check_phase", [0.0, 2.5, 4.999])
async def test_a_lead_whose_host_stops_writing_is_reported_unavailable_within_35_seconds(check_phase: float) -> None:
    """The lead's host crashes right after a liveness write; the gateway marks the lead unavailable in time.

    The host's own upkeep code writes the liveness records (``MessageHost.publish_liveness``) on a
    fake clock, the team's live view checks every 5 seconds as its loop does (``TeamAgents._rebuild``,
    at any phase against the writes), and the gateway's team view hands the lead's leaving to the
    manager client, which marks it unavailable. As with the presence heartbeat, that is about 30
    seconds after the host stopped: its record's 30 seconds plus at most one 5-second check.
    """

    start = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
    now = [start]
    team_host = SimpleNamespace(clock=lambda: now[0])
    agents = TeamAgents(team_host, "team_test")
    agents._agents["lead_1"] = _Held(1, "agent", SimpleNamespace(
        signer=SimpleNamespace(runtime=LEAD_HOST),
        body=SimpleNamespace(state="idle", session="session_lead", agent_type="TeamArchitect",
                             display_name="TeamLead", accepting_work=True, since=format_timestamp(start))))
    agents._placements["lead_1"] = _Held(1, "placement", SimpleNamespace(body=SimpleNamespace(host=LEAD_HOST,
                                                                                              host_epoch=1)))
    agents._roles["session_lead"] = "lead"

    class _HostRecords:
        """The lead host's writes, as the gateway's watch of ``UA1_HOSTS`` takes them in."""

        def signed(self, record, key, body, *, sequence, issued_at, expires_at):
            return {"sequence": sequence, "expires_at": format_timestamp(expires_at)}

        async def put(self, record, key, document):
            agents._hosts[key] = _Held(document["sequence"], "host", SimpleNamespace(
                document=SimpleNamespace(expires_at=document["expires_at"]),
                body=SimpleNamespace(certificate=SimpleNamespace(membership_epoch=1))))
            agents._rebuild()

    lead_host = MessageHost(layout=None, profile=SimpleNamespace(runtime=LEAD_HOST), nc=None, clock=lambda: now[0])
    lead_host.records = _HostRecords()
    lead_host.liveness_body = lambda state: {"state": state}

    unavailable: list = []
    client = _client(_FakeManagerRuntime(), settings=_settings(manager_id=None), unavailable=unavailable)

    async def on_lead(status: AgentStatus) -> None:
        await client.apply_manager_status(status, source="agent_records")

    view = GatewayTeamView("team_test", on_status=lambda status, reported_at, source: None, on_lead=on_lead)
    view._agents = agents
    agents.listen(view._changed)

    async def apply_lead_changes() -> None:
        # What GatewayTeamView._apply_leads does on its own loop, in order.
        while not view._leads.empty():
            await on_lead(view._leads.get_nowait())

    async def advance_to(moment: datetime) -> None:
        """Move the clock on, running the view's 5-second checks that fall in between."""

        nonlocal next_check
        while next_check <= moment:
            now[0] = next_check
            agents._rebuild()
            await apply_lead_changes()
            next_check += timedelta(seconds=RECHECK_SECONDS)
        now[0] = moment

    next_check = start + timedelta(seconds=check_phase)
    agents.ready.set()
    view.ready.set()
    # The host is up and writes every 10 seconds for a minute: the lead stays available throughout.
    for write in range(7):
        await advance_to(start + write * LIVENESS_INTERVAL)
        await lead_host.publish_liveness("accepting")
        await apply_lead_changes()
    last_write = now[0]
    assert client.manager_id == "lead_1" and unavailable == []

    # The host crashes right after that write. Nothing else is written; only the checks run.
    reported_at = None
    while reported_at is None and now[0] < last_write + timedelta(minutes=2):
        await advance_to(now[0] + timedelta(seconds=0.5))
        if unavailable:
            reported_at = now[0]
    assert reported_at is not None and [item.manager_id for item in unavailable] == ["lead_1"]
    assert client.manager_id is None and view.lead() is None
    gone_after = (reported_at - last_write).total_seconds()
    assert LIVENESS_LIFETIME.total_seconds() <= gone_after <= 35.0


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
