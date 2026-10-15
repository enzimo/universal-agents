"""Failed delivery and report retries against real JetStream and SQLite.

The original PR's small-message/custom-callback tests missed the actual agent
catch path, total header size, and persisted notices surviving stream resets.
These tests exercise those boundaries without a model or an external chat.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.dead_letter_watch import DeadLetterWatchState, report_dead_letters
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_messages.transport_codec import encode_envelope
from custom_transport.dead_letters import REASON_HANDLER_FAILED, WATCH_CONSUMER_ROUTE
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_tools.comms_tools import TeamComms
from id_generation import new_id
from storage.access_repository import AccessRepository
from tests.test_live_undecodable_messages import (
    _adapter,
    _consumer_settled,
    _delete_streams,
    _task_bytes,
    _unknown_type_bytes,
    _wait_until,
)
from tests.test_status_page import _config
from tests.test_team_lead_delivery import lead_delivery_probe

pytestmark = [pytest.mark.live, pytest.mark.nats, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def adapter():
    transport = _adapter(new_id("dead_letter"), max_deliver=1)
    await transport.connect(reconcile_topology=True)
    try:
        yield transport
    finally:
        await _delete_streams(transport)
        await transport.close()


async def _mailbox(adapter: NatsTransportAdapter, handler=None) -> str:
    async def acknowledge(delivery):
        await delivery.ack()

    durable = adapter.mapper.durable_name("regression_mailbox")
    await adapter.subscribe_route(
        name="regression_mailbox",
        routing_key="agent_x",
        handler=handler or acknowledge,
        durable_name=durable,
    )
    return durable


async def _publish_and_settle(adapter, durable, payload, *, headers=None) -> None:
    await adapter._js.publish(
        adapter.mapper.agent_work_subject("agent_x"), payload, headers=headers
    )
    assert await _wait_until(
        lambda: _consumer_settled(adapter._js, adapter.mapper.work_stream_name(), durable),
        timeout=10,
    )


def _lead(adapter, tmp_path, *, chat: bool):
    team_id = adapter.mapper.team_id
    config = _config(tmp_path)
    config.manager.team_id = team_id
    config.gateway.team_id = team_id
    config.transport.storage = "memory"
    config.manager.response_queue_name = f"{team_id}_gateway_status"
    config.delivery.default_target.channel_name = "cli" if chat else "telegram"
    config.delivery.default_target.chat_id = ""
    config.save()
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity(
        agent_id="lead", agent_name="lead", team_id=team_id, named_agent_type="TeamArchitect"
    )
    AccessRepository(manager.storage_settings).record_person(
        TeamPersonV1(
            team_id=team_id,
            person_id=config.user.canonical_user_id,
            roles=(PERSON_ROLE_ADMIN,),
            standing=PersonStanding.ACTIVE,
            changed_at_utc=datetime.now(timezone.utc),
        )
    )
    return SimpleNamespace(
        id="lead", name="lead", team_id=team_id, is_team_lead=True,
        instance_config_path=config.layout.instance_config_path,
        task_manager=manager,
        team_comms=TeamComms(team_id=team_id, nats_adapter=adapter),
        _dead_letter_watch=DeadLetterWatchState(),
        _ensure_nats_transport_adapter=lambda: adapter,
        _logger=logging.getLogger("test.live_dead_letter_reporting"),
    )


def _notices(lead):
    return [
        notice for notice, _ in AccessRepository(lead.task_manager.storage_settings).notices_for(
            lead.team_id, person_id="administrator", roles=(PERSON_ROLE_ADMIN,)
        )
    ]


async def _reports_settled(adapter) -> bool:
    await adapter._nc.flush()
    return await _consumer_settled(
        adapter._js, adapter.mapper.dead_letter_stream_name(),
        adapter.mapper.durable_name(WATCH_CONSUMER_ROUTE),
    )


async def test_actual_agent_failure_is_preserved_before_ending(adapter):
    adapter.config = replace(adapter.config, max_deliver=3)
    agent = lead_delivery_probe(team_id=adapter.mapper.team_id)
    agent._should_exit.set()  # No model or pending-work turn is started.
    agent._ensure_nats_transport_adapter = lambda: adapter
    agent._handle_task_request.side_effect = RuntimeError("task storage failed")
    durable = adapter.mapper.durable_name("actual_agent")
    await adapter.subscribe_route(
        name="actual_agent", routing_key=agent.team_id,
        handler=agent.on_nats_delivery, durable_name=durable,
    )
    payload = encode_envelope(MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="gateway", to_routing_path=agent.team_id),
        msg_meta=MessageMeta(to_named_agent_type="TeamArchitect"),
        msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="failed request"),
    ))
    await adapter._js.publish(adapter.mapper.lead_work_subject(), payload)
    assert await _wait_until(
        lambda: _consumer_settled(adapter._js, adapter.mapper.work_stream_name(), durable),
        timeout=10,
    )
    assert agent._handle_task_request.await_count == 3
    [record] = await adapter.fetch_dead_letters(consumer_route=WATCH_CONSUMER_ROUTE)
    assert record.reason == REASON_HANDLER_FAILED
    raw = await adapter._js.get_last_msg(adapter.mapper.dead_letter_stream_name(), record.raw_subject)
    assert raw.data == payload
    await record.ack()
    assert (await adapter._js.stream_info(adapter.mapper.work_stream_name())).state.messages == 0

    # The same production callback still handles ordinary subsequent work.
    agent._handle_task_request.side_effect = None
    await adapter._js.publish(adapter.mapper.lead_work_subject(), payload)
    assert await _wait_until(
        lambda: _consumer_settled(adapter._js, adapter.mapper.work_stream_name(), durable),
        timeout=10,
    )
    assert agent._handle_task_request.await_count == 4
    assert adapter.stopped_receivers() == ()


@pytest.mark.parametrize("header_kind", ["none", "trace", "large"])
async def test_largest_accepted_message_is_retained_without_closing_connection(adapter, header_kind):
    durable = await _mailbox(adapter)
    if header_kind == "none":
        headers, overhead = None, 0
    elif header_kind == "trace":
        headers = {"trace": "t-1"}
        overhead = len(b"NATS/1.0\r\ntrace: t-1\r\n\r\n")
    else:
        # Also prove original header values cannot inflate the diagnostic record.
        # JetStream also limits the header block to 64 KiB.
        headers = {"message_id": "h" * (64 * 1024 - 100)}
        overhead = len(b"NATS/1.0\r\nmessage_id: \r\n\r\n") + len(headers["message_id"])
    payload = b"x" * (adapter._nc.max_payload - overhead)
    await _publish_and_settle(adapter, durable, payload, headers=headers)

    [record] = await adapter.fetch_dead_letters(consumer_route=WATCH_CONSUMER_ROUTE)
    raw = await adapter._js.get_last_msg(adapter.mapper.dead_letter_stream_name(), record.raw_subject)
    assert raw.data == payload
    if headers:
        assert all(raw.headers[key] == value for key, value in headers.items())
    assert not adapter._nc.is_closed
    assert len(record.message_id) <= 120
    await record.ack()
    # Both a normal publication and the pull loop must remain usable.
    await _publish_and_settle(adapter, durable, _task_bytes("subsequent valid message"))
    assert adapter.stopped_receivers() == ()


async def test_inbox_success_drains_reports_when_real_chat_publish_fails(adapter, tmp_path, caplog):
    durable = await _mailbox(adapter)
    await _publish_and_settle(adapter, durable, _unknown_type_bytes())
    lead = _lead(adapter, tmp_path, chat=True)
    # The independent Inbox and dead-letter stream work while the response
    # stream is unavailable. This is a real publish failure, not a mock.
    await adapter._js.delete_stream(adapter.mapper.work_stream_name())

    await report_dead_letters(lead)

    assert len(_notices(lead)) == 1
    assert lead._dead_letter_watch.reported_at is not None
    assert await _reports_settled(adapter)
    assert "event=dead_letter_notice_chat_failed" in caplog.text


async def test_recreated_memory_stream_does_not_reuse_persisted_notice(adapter, tmp_path):
    lead = _lead(adapter, tmp_path, chat=False)
    payload = _unknown_type_bytes()
    durable = await _mailbox(adapter)
    await _publish_and_settle(adapter, durable, payload)
    await report_dead_letters(lead)
    [before] = _notices(lead)
    assert await _reports_settled(adapter)

    # Keep real SQLite history, but recreate both memory streams and the lead's
    # process state. Even identical bytes at the same source sequence are new.
    await adapter._js.delete_stream(adapter.mapper.dead_letter_stream_name())
    await adapter._js.delete_stream(adapter.mapper.work_stream_name())
    await adapter.close()
    replacement = NatsTransportAdapter(config=adapter.config, mapper=adapter.mapper)
    try:
        await replacement.connect(reconcile_topology=True)
        durable = await _mailbox(replacement)
        lead.team_comms = TeamComms(team_id=lead.team_id, nats_adapter=replacement)
        lead._ensure_nats_transport_adapter = lambda: replacement
        lead._dead_letter_watch = DeadLetterWatchState()
        await _publish_and_settle(replacement, durable, payload)
        await report_dead_letters(lead)

        notices = _notices(lead)
        assert len(notices) == 2
        assert len({notice.notice_id for notice in notices}) == 2
        assert any(notice.notice_id != before.notice_id for notice in notices)
        assert await _reports_settled(replacement)
    finally:
        await _delete_streams(replacement)
        await replacement.close()


async def test_chat_can_report_when_inbox_storage_fails(adapter, tmp_path, monkeypatch):
    durable = await _mailbox(adapter)
    await _publish_and_settle(adapter, durable, _unknown_type_bytes())
    lead = _lead(adapter, tmp_path, chat=True)

    def unavailable_inbox(self, notice):
        raise RuntimeError("Inbox storage unavailable")

    monkeypatch.setattr(AccessRepository, "create_notice_if_absent", unavailable_inbox)
    await report_dead_letters(lead)

    assert lead._dead_letter_watch.reported_at is not None
    assert await _reports_settled(adapter)
    stream = await adapter._js.stream_info(adapter.mapper.work_stream_name())
    assert stream.state.messages == 1
    response = await adapter._js.get_msg(adapter.mapper.work_stream_name(), seq=stream.state.last_seq)
    assert b"messages_set_aside" in response.data
