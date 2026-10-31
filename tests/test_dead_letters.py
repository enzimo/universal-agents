"""Set-aside (dead-letter) messages, the team lead's report on them, and stopped receivers.

Since 0.8.1 a receiver ends a message it cannot read or handle on its last
allowed delivery. Before ending it, the receiver now copies it to the
dead-letter stream (kept three days), the team lead tells administrators in
their Inbox and the owner's chat, and a receive loop that ends anyway is shown
on the Status page. Fake NATS objects here; ``test_live_undecodable_messages``
checks the copies against a real server.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.dead_letter_watch import (
    REPORT_QUIET_MINUTES,
    DeadLetterWatchState,
    dead_letter_notice,
    report_dead_letters,
)
from custom_agents.status_findings import agent_status_findings
from custom_agents.stuck_work import ChatRoute
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.contracts.status_v1 import StatusSeverity
from custom_transport.base import FetchPolicy
from custom_transport.dead_letters import (
    HEADER_COPY_ID,
    HEADER_MESSAGE_CLASS,
    HEADER_REASON,
    HEADER_RECEIVER,
    HEADER_RAW_SUBJECT,
    REASON_HANDLER_FAILED,
    REASON_UNDECODABLE,
    DeadLetterRecord,
    StoppedReceiver,
)
from custom_transport.nats_adapter import NatsTransportConfig
from custom_transport.nats_pull import ENDED, RETURNED, PullRunner, return_or_end
from custom_transport.nats_topology import (
    DEAD_LETTER_MAX_AGE_SECONDS,
    DEAD_LETTER_MAX_BYTES,
    NatsStreamTopology,
)
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version
from storage.access_repository import AccessRepository
from tests.test_nats_pull_delivery import (
    _adapter,
    _failing_runner,
    _JsMessage,
    _run_briefly,
    _Subscription,
    _valid_envelope_json,
)

# -- Ending a message copies it first -------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_is_set_aside_only_on_its_last_delivery() -> None:
    set_aside = AsyncMock(return_value=True)
    early, last = _JsMessage("early", delivered=2), _JsMessage("last", delivered=5)

    assert await return_or_end(early, max_deliver=5, set_aside=set_aside) == RETURNED
    set_aside.assert_not_awaited()
    assert await return_or_end(last, max_deliver=5, set_aside=set_aside) == ENDED
    set_aside.assert_awaited_once()
    assert last.terminated


@pytest.mark.asyncio
async def test_a_message_still_ends_when_setting_it_aside_fails() -> None:
    last = _JsMessage("last", delivered=3)

    outcome = await return_or_end(last, max_deliver=3, set_aside=AsyncMock(side_effect=RuntimeError("down")))

    assert outcome == ENDED and last.terminated


@pytest.mark.asyncio
async def test_a_handler_that_fails_on_the_last_delivery_sets_the_message_aside() -> None:
    last = _JsMessage("bad", delivered=5)
    runner = _failing_runner(_Subscription([[last]]), fail_on={"bad"}, handled=[], max_deliver=5)
    calls: list[tuple] = []

    async def set_aside(message, reason, error) -> bool:
        calls.append((message.name, reason, error))
        return True

    runner._set_aside = set_aside
    await _run_briefly(runner)

    assert calls == [("bad", REASON_HANDLER_FAILED, "RuntimeError")]
    assert last.terminated


# -- The copy -------------------------------------------------------------------------


class _PublishingJetStream:
    def __init__(self, error: Exception | None = None) -> None:
        self.published: list[tuple[str, bytes, dict]] = []
        self.error = error

    async def publish(self, subject, payload, headers=None):
        if self.error is not None:
            raise self.error
        self.published.append((subject, payload, dict(headers or {})))


@pytest.mark.asyncio
async def test_an_unreadable_message_is_copied_with_why_and_from_where(caplog) -> None:
    import json

    document = _valid_envelope_json()
    document["msg_content"] = {"message_class": "FutureMessageV9", "secret_detail": "do-not-log-me"}
    payload = json.dumps(document).encode()
    adapter = _adapter()
    adapter._js = _PublishingJetStream()
    adapter._nc = SimpleNamespace(max_payload=1024 * 1024)
    message = _JsMessage("unknown", data=payload, delivered=5, subject="team_t.agent.agent_a.work")
    message.headers = {"Nats-Msg-Id": "original-id", "trace": "t-1"}
    message.metadata.stream = "TEAM_T_WORK"

    with caplog.at_level(logging.WARNING, logger="test.adapter"):
        await adapter._decode_or_settle(
            message,
            subscription_name="mailbox",
            logical_route="agent_a",
            max_deliver=5,
            ack_wait_seconds=30.0,
            jetstream=True,
        )

    [(raw_subject, copied, original_headers), (subject, diagnostic_body, headers)] = adapter._js.published
    assert raw_subject == adapter.mapper.dead_letter_raw_subject(headers[HEADER_COPY_ID])
    assert subject == adapter.mapper.dead_letter_notice_subject(headers[HEADER_COPY_ID])
    assert copied == payload
    assert original_headers == {"trace": "t-1"}
    assert diagnostic_body == b""
    assert headers[HEADER_RAW_SUBJECT] == raw_subject
    assert headers["Nats-Msg-Id"] == f"dead-letter:{headers[HEADER_COPY_ID]}"
    assert headers[HEADER_REASON] == REASON_UNDECODABLE
    assert headers[HEADER_MESSAGE_CLASS] == "FutureMessageV9"
    assert headers[HEADER_RECEIVER] == "mailbox"
    assert headers["UA-Release"] == release_version()
    assert headers["message_id"] == document["msg_header"]["message_id"]
    assert message.terminated
    text = next(r.getMessage() for r in caplog.records if "event=nats_message_dead_lettered" in r.getMessage())
    assert "do-not-log-me" not in text


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_stored_is_logged_and_the_message_still_ends(caplog) -> None:
    adapter = _adapter()
    adapter._js = _PublishingJetStream(error=RuntimeError("stream full"))
    adapter._nc = SimpleNamespace(max_payload=1024 * 1024)
    message = _JsMessage("garbage", data=b"{not json", delivered=5)

    with caplog.at_level(logging.ERROR, logger="test.adapter"):
        await adapter._decode_or_settle(
            message, subscription_name="mailbox", logical_route="agent_a", max_deliver=5,
            ack_wait_seconds=30.0, jetstream=True,
        )

    assert message.terminated
    assert any("event=nats_dead_letter_failed" in r.getMessage() for r in caplog.records)


def test_diagnostics_and_raw_messages_have_distinct_subjects() -> None:
    mapper = NatsSubjectMapper(team_id="team_t")

    assert mapper.dead_letter_raw_subject("copy-1") == "team_t.deadletter.raw.copy-1"
    assert mapper.dead_letter_notice_subject("copy-1") == "team_t.deadletter.notice.copy-1"
    assert mapper.dead_letter_notice_subjects() == "team_t.deadletter.notice.*"


def test_the_dead_letter_stream_keeps_copies_three_days_within_a_size_limit() -> None:
    from nats.js.api import RetentionPolicy

    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="team_t"))
    streams = {stream.name: stream for stream in topology.specifications()}
    dead_letters = streams[topology.mapper.dead_letter_stream_name()]

    assert dead_letters.subjects == ["team_t.deadletter.>"]
    assert dead_letters.retention is RetentionPolicy.LIMITS
    assert dead_letters.max_age == DEAD_LETTER_MAX_AGE_SECONDS == 3 * 24 * 60 * 60
    assert dead_letters.max_bytes == DEAD_LETTER_MAX_BYTES
    assert dead_letters.max_msgs_per_subject == 1
    # No other stream may claim the copies' subjects.
    others = [s for name, s in streams.items() if name != dead_letters.name]
    assert not any(subject.startswith("team_t.deadletter") for stream in others for subject in stream.subjects)


# -- A receive loop that stops --------------------------------------------------------


def _runner_that_reports(stopped: list) -> PullRunner:
    return PullRunner(
        name="mailbox",
        jetstream=None,
        stream_name="S",
        consumer_config=SimpleNamespace(durable_name="d", max_deliver=5),
        policy=FetchPolicy.CONTINUOUS,
        ready=lambda: True,
        callback=AsyncMock(),
        is_closing=lambda: False,
        logger=logging.getLogger("test.pull"),
        on_stopped=lambda name, error: stopped.append((name, type(error).__name__ if error else None)),
    )


@pytest.mark.asyncio
async def test_a_loop_that_ends_on_its_own_is_reported_and_not_restarted(caplog) -> None:
    stopped: list = []
    runner = _runner_that_reports(stopped)

    async def broken() -> None:
        raise RuntimeError("a defect in the loop")

    task = asyncio.create_task(broken())
    await asyncio.gather(task, return_exceptions=True)
    with caplog.at_level(logging.ERROR, logger="test.pull"):
        runner._loop_done(task)

    assert stopped == [("mailbox", "RuntimeError")]
    assert any("event=nats_pull_loop_stopped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_loop_asked_to_stop_is_not_reported() -> None:
    stopped: list = []
    runner = _runner_that_reports(stopped)
    runner._subscription = _Subscription([])
    runner._task = asyncio.create_task(runner._run())
    runner._task.add_done_callback(runner._loop_done)

    await asyncio.sleep(0.05)
    await runner.unsubscribe()
    await asyncio.sleep(0)

    assert stopped == []


def test_agents_and_the_gateway_show_a_stopped_receiver_as_a_problem() -> None:
    receiver = StoppedReceiver(subscription="mailbox", stopped_at=datetime.now(timezone.utc), error_type="KeyError")
    agent = SimpleNamespace(
        named_agent_type="TeamArchitect", name="Nova", team_id="team_t", is_team_lead=True,
        _memory_graph_degraded_reason="", _skill_catalog_runtime_state=SimpleNamespace(last_error=""),
        _strands_parts=SimpleNamespace(sync=SimpleNamespace(servers=SimpleNamespace(failures=lambda: {}))),
        team_comms=SimpleNamespace(nats_adapter=SimpleNamespace(stopped_receivers=lambda: (receiver,))),
    )

    [finding] = [item for item in agent_status_findings(agent) if item.finding_id.startswith("receiver_stopped")]

    assert finding.severity is StatusSeverity.PROBLEM
    assert finding.finding_id == "receiver_stopped:TeamArchitect (Nova):mailbox"
    assert "KeyError" in (finding.details or "")


@pytest.mark.asyncio
async def test_the_gateway_status_page_lists_its_own_stopped_receivers(tmp_path) -> None:
    from tests.test_status_page import _config, _page

    page = _page(_config(tmp_path))
    receiver = StoppedReceiver(subscription="gateway_responses", stopped_at=datetime.now(timezone.utc), error_type=None)
    page._stopped_receivers = lambda: (receiver,)

    report = await page.report()

    assert "receiver_stopped:Gateway:gateway_responses" in [finding.finding_id for finding in report.findings]


# -- The team lead tells administrators -----------------------------------------------


class _Copies:
    """The dead-letter reader: hands out records until each is acknowledged."""

    def __init__(self, count: int, *, waiting_after: int = 0) -> None:
        self.unacked: dict[int, DeadLetterRecord] = {}
        for sequence in range(1, count + 1):
            self.unacked[sequence] = self._record(sequence, waiting_after if sequence == count else 0)
        self.fetches = 0

    def _record(self, sequence: int, waiting_after: int) -> DeadLetterRecord:
        async def ack() -> None:
            self.unacked.pop(sequence, None)

        return DeadLetterRecord.from_headers(
            {
                HEADER_COPY_ID: f"copy-{sequence}",
                HEADER_REASON: REASON_UNDECODABLE,
                HEADER_MESSAGE_CLASS: "FutureMessageV9",
                HEADER_RECEIVER: "mailbox",
                "UA-Release": "0.8.0",
                "UA-Dead-Lettered-At": "2026-10-07T03:00:00+00:00",
                "message_id": f"m-{sequence}",
            },
            stream_sequence=sequence,
            ack=ack,
            waiting_after=waiting_after,
        )

    async def fetch_dead_letters(self, *, consumer_route: str, batch: int) -> list[DeadLetterRecord]:
        self.fetches += 1
        return list(self.unacked.values())[:batch]


@pytest.fixture
def lead(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity(agent_id="lead", agent_name="lead", team_id="team", named_agent_type="TeamArchitect")
    copies = _Copies(2)
    adapter = SimpleNamespace(
        connected=True,
        fetch_dead_letters=copies.fetch_dead_letters,
        mapper=NatsSubjectMapper(team_id="team"),
    )
    return SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        is_team_lead=True,
        instance_config_path=None,
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _dead_letter_watch=DeadLetterWatchState(),
        _ensure_nats_transport_adapter=lambda: adapter,
        _logger=logging.getLogger("test.dead_letter_watch"),
        copies=copies,
    )


def _admin_notices(lead) -> list:
    rows = AccessRepository(lead.task_manager.storage_settings).notices_for(
        "team", person_id="someone", roles=(PERSON_ROLE_ADMIN,)
    )
    return [notice for notice, _read in rows]


def _owner_chat(monkeypatch) -> None:
    route = ChatRoute("gateway.instance.responses", GatewayContext(canonical_user_id="owner", channel_name="cli"))
    monkeypatch.setattr("custom_agents.dead_letter_watch._owner_chat", lambda agent: route)


@pytest.mark.asyncio
async def test_administrators_hear_about_set_aside_messages_in_the_inbox_and_chat(lead, monkeypatch) -> None:
    _owner_chat(monkeypatch)

    await report_dead_letters(lead)

    [notice] = _admin_notices(lead)
    assert notice.kind is AccessNoticeKind.MESSAGES_SET_ASIDE
    assert notice.recipient_roles == (PERSON_ROLE_ADMIN,)
    assert notice.notice_id.startswith("dead_letters:")
    assert "FutureMessageV9 (m-1) for mailbox" in notice.summary and "release 0.8.0" in notice.summary
    lead.team_comms.send_message.assert_awaited_once()
    sent = lead.team_comms.send_message.await_args.kwargs["message"]
    assert sent.msg_content.reason == "messages_set_aside"
    assert sent.msg_meta.msg_correlation_id == notice.notice_id
    assert lead.copies.unacked == {}


@pytest.mark.asyncio
async def test_without_a_chat_the_inbox_notice_is_enough(lead) -> None:
    await report_dead_letters(lead)

    assert len(_admin_notices(lead)) == 1
    lead.team_comms.send_message.assert_not_awaited()
    assert lead.copies.unacked == {}


@pytest.mark.asyncio
async def test_a_report_that_reached_nobody_is_tried_again(lead, monkeypatch, caplog) -> None:
    monkeypatch.setattr(AccessRepository, "create_notice_if_absent", lambda self, notice: 1 / 0)

    with caplog.at_level(logging.ERROR):
        await report_dead_letters(lead)

    assert len(lead.copies.unacked) == 2
    assert lead._dead_letter_watch.reported_at is None
    assert any("event=dead_letter_notice_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_batch_already_in_the_inbox_is_acknowledged_without_a_second_notice(lead) -> None:
    # The notice was stored, then acknowledging failed (the process stopped, say).
    acks = {sequence: record.ack for sequence, record in lead.copies.unacked.items()}
    lead.copies.unacked = {
        sequence: replace(record, ack=AsyncMock(side_effect=RuntimeError("stopped")))
        for sequence, record in lead.copies.unacked.items()
    }
    await report_dead_letters(lead)
    assert len(_admin_notices(lead)) == 1 and len(lead.copies.unacked) == 2
    lead.copies.unacked = {
        sequence: replace(record, ack=acks[sequence]) for sequence, record in lead.copies.unacked.items()
    }

    await report_dead_letters(lead)

    assert len(_admin_notices(lead)) == 1
    assert lead.copies.unacked == {}


@pytest.mark.asyncio
async def test_after_a_report_the_next_waits_and_then_covers_everything_set_aside(lead) -> None:
    await report_dead_letters(lead)
    lead.copies.unacked = {3: lead.copies._record(3, 0), 4: lead.copies._record(4, 0)}

    await report_dead_letters(lead)
    assert lead.copies.fetches == 1 and len(lead.copies.unacked) == 2

    lead._dead_letter_watch.reported_at -= timedelta(minutes=REPORT_QUIET_MINUTES)
    await report_dead_letters(lead)

    notices = _admin_notices(lead)
    assert len({notice.notice_id for notice in notices}) == 2
    assert any("m-3" in notice.summary and "m-4" in notice.summary for notice in notices)
    assert lead.copies.unacked == {}


@pytest.mark.asyncio
async def test_the_watch_never_connects_and_never_fails_the_scheduler_pass(lead, caplog) -> None:
    adapter = lead._ensure_nats_transport_adapter()
    adapter.connected = False
    await report_dead_letters(lead)
    assert lead.copies.fetches == 0

    adapter.connected = True
    adapter.fetch_dead_letters = AsyncMock(side_effect=RuntimeError("broker gone"))
    with caplog.at_level(logging.ERROR):
        await report_dead_letters(lead)

    assert any("event=dead_letter_watch_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_reader_does_not_wait_when_nothing_was_set_aside() -> None:
    adapter = _adapter()
    subscription = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("must not wait for messages")))
    adapter._dead_letter_subscriptions[adapter.mapper.durable_name("dead_letter_watch")] = subscription
    adapter._js = SimpleNamespace(
        consumer_info=AsyncMock(return_value=SimpleNamespace(num_pending=0, num_ack_pending=0))
    )

    assert await adapter.fetch_dead_letters(consumer_route="dead_letter_watch") == []
    assert await _adapter().fetch_dead_letters(consumer_route="dead_letter_watch") == []  # not connected


@pytest.mark.asyncio
async def test_only_the_team_lead_reports(lead) -> None:
    lead.is_team_lead = False

    await report_dead_letters(lead)

    assert lead.copies.fetches == 0 and _admin_notices(lead) == []


def test_the_notice_names_what_was_set_aside_and_what_is_still_waiting() -> None:
    copies = _Copies(12, waiting_after=30)
    records = list(copies.unacked.values())

    message = dead_letter_notice(records, stream="TEAM_DEAD_LETTER")

    assert message.title == "12 message(s) could not be handled and were set aside"
    assert "- and 2 more." in message.text
    assert "30 more were set aside after these" in message.text
    assert "TEAM_DEAD_LETTER" in message.text and "up to 3 days" in message.text
    assert "another release" in message.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["route", "publish"])
async def test_inbox_success_allows_acknowledgement_despite_chat_failure(
    lead, monkeypatch, caplog, failure
) -> None:
    _owner_chat(monkeypatch)
    if failure == "route":
        def broken_route(agent):
            raise RuntimeError("configuration unavailable")
        monkeypatch.setattr("custom_agents.dead_letter_watch._owner_chat", broken_route)
    else:
        lead.team_comms.send_message.side_effect = RuntimeError("chat publish failed")

    with caplog.at_level(logging.ERROR):
        await report_dead_letters(lead)

    assert len(_admin_notices(lead)) == 1
    assert lead.copies.unacked == {}
    assert lead._dead_letter_watch.reported_at is not None
    assert "event=dead_letter_notice_chat_failed" in caplog.text


@pytest.mark.asyncio
async def test_failed_inbox_and_chat_leave_copies_pending(lead, monkeypatch) -> None:
    def broken_store(self, notice):
        raise RuntimeError("Inbox unavailable")

    monkeypatch.setattr(AccessRepository, "create_notice_if_absent", broken_store)
    _owner_chat(monkeypatch)
    lead.team_comms.send_message.side_effect = RuntimeError("chat publish failed")

    await report_dead_letters(lead)

    assert len(lead.copies.unacked) == 2
    assert lead._dead_letter_watch.reported_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("roles", "standing", "can_receive"),
    [
        ((PERSON_ROLE_ADMIN,), PersonStanding.ACTIVE, True),
        (("owner",), PersonStanding.ACTIVE, True),
        (("member",), PersonStanding.ACTIVE, False),
        ((), PersonStanding.DISABLED, False),
        ((), PersonStanding.REMOVED, False),
        ((), None, False),
    ],
)
async def test_chat_uses_current_administrator_standing(
    lead, tmp_path, roles, standing, can_receive
) -> None:
    from tests.test_status_page import _config

    config = _config(tmp_path)
    config.manager.team_id = lead.team_id
    config.gateway.team_id = lead.team_id
    config.manager.response_queue_name = "gateway.status.responses"
    config.delivery.default_target.channel_name = "cli"
    config.save()
    lead.instance_config_path = config.layout.instance_config_path
    if standing is not None:
        AccessRepository(lead.task_manager.storage_settings).record_person(
            TeamPersonV1(
                team_id=lead.team_id,
                person_id=config.user.canonical_user_id,
                roles=roles,
                standing=standing,
                changed_at_utc=datetime.now(timezone.utc),
            )
        )

    await report_dead_letters(lead)

    assert lead.team_comms.send_message.await_count == int(can_receive)
    assert len(_admin_notices(lead)) == 1
    assert lead.copies.unacked == {}


@pytest.mark.asyncio
async def test_copy_identity_distinguishes_batches_at_the_same_stream_sequence(lead) -> None:
    await report_dead_letters(lead)
    first_id = _admin_notices(lead)[0].notice_id
    # A broker restart resets positions but a different original keeps its own
    # stable copy identity. Persisted Inbox history must not hide its report.
    lead.copies.unacked = {
        1: replace(lead.copies._record(1, 0), copy_id="new-copy", message_id="new-message")
    }
    lead._dead_letter_watch = DeadLetterWatchState()

    await report_dead_letters(lead)

    notices = _admin_notices(lead)
    assert len(notices) == 2
    assert any(notice.notice_id != first_id and "new-message" in notice.summary for notice in notices)
    assert lead.copies.unacked == {}
