"""Several agent questions waiting in one chat, through the real gateway.

Two requests can wait on the person in the same chat: a request whose response
wait ended (a timeout, a restart, or a credential form) keeps working while the
person starts another one, and scheduled work asks in the chat it reports to.
These tests drive the real ``UserGateway`` with its real SQLite store, the chat
bridge and Web Chat; only the lead is replaced by a transport that records what
the gateway sends and answers its task-status checks.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.pending_questions import (
    ALREADY_ANSWERED_TEXT,
    NO_LONGER_WAITING_TEXT,
    QUESTION_WAITING_INTRO_TEXT,
    QUESTION_WAITS_IN_LINE_TEXT,
)
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task, TaskAnswer, TaskQuery, TaskStatus
from custom_messages.enums import MessageMetaType, RequesterType
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp

TELEGRAM_ID = "100200300"


class _RecordingLead:
    """Stands in for the lead: records answers, may reply, answers status checks."""

    # Read by the gateway's manager client: status checks run only while the
    # lead connection is up.
    connected = True

    def __init__(self) -> None:
        self.durable: list[MessageEnvelope] = []
        self.on_answer = None
        self.gateway: UserGateway | None = None
        # Tasks the lead no longer knows (finished and forgotten, or lost).
        self.unknown_tasks: set[str] = set()

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        self.durable.append(envelope)
        if self.on_answer is not None:
            await self.on_answer(envelope)

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        request = envelope.msg_content
        if not isinstance(request, TaskStatus) or self.gateway is None:
            return
        manager_id = self.gateway.config.manager.manager_agent_id
        await self.gateway._manager_client.get_control_channel(
            envelope.msg_meta.msg_correlation_id
        ).put(
            MessageEnvelope(
                msg_content=request.model_copy(
                    update={
                        "status_type": MessageMetaType.RESPONSE,
                        "is_known_task": request.task_id not in self.unknown_tasks,
                        "responder_agent_id": manager_id,
                    }
                ),
                msg_header=MessageHeader(from_agent_id=manager_id),
                msg_meta=envelope.msg_meta,
            )
        )

    def answers(self) -> list[TaskQuery]:
        return [
            envelope.msg_content
            for envelope in self.durable
            if isinstance(envelope.msg_content, TaskQuery)
            and envelope.msg_content.query_type == MessageMetaType.RESPONSE
        ]

    def answer_ids(self) -> list[str]:
        return [answer.task_query_id for answer in self.answers()]


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
        manager_agent_id="manager_1234",
    )
    config.delivery.quiet_hours.enabled = False
    config.save()
    return config


class _Chat:
    """One chat with the real gateway, its store and the recording lead."""

    def __init__(self, tmp_path: Path, *, channel: str = "telegram") -> None:
        self.tmp_path = tmp_path
        self.config = _config(tmp_path)
        self.lead = _RecordingLead()
        self.shown: list = []
        self.gateway = self._start()
        self.session = (
            self.gateway.application.start_new_session(
                channel_name="telegram", user_id=TELEGRAM_ID, chat_id=TELEGRAM_ID
            )
            if channel == "telegram"
            else None
        )

    def _start(self) -> UserGateway:
        gateway = UserGateway(
            self.config,
            output_sink=None,
            state_store=GatewayStateStore(str(self.tmp_path / "state.db")),
            event_sink=self.shown.append,
            manager_transport=self.lead,
        )
        gateway._manager_client.set_runtime(self.lead, use_for_outbound=False)
        self.lead.gateway = gateway
        return gateway

    def restart(self) -> None:
        self.gateway = self._start()

    @property
    def store(self) -> GatewayStateStore:
        return self.gateway.state_store

    def meta(self, correlation_id: str) -> MessageMeta:
        return MessageMeta(
            msg_correlation_id=correlation_id,
            conversation_id=self.session.conversation_id,
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key=self.session.session_key,
                canonical_user_id=self.config.user.canonical_user_id,
                delivery_mode="interactive",
            ),
        )

    def record_request(self, correlation_id: str, text: str) -> None:
        # The request's response wait already ended (timeout or restart).
        self.store.record_request(
            correlation_id=correlation_id,
            session_key=self.session.session_key,
            request_kind="interactive",
            request_text=text,
            conversation_id=self.session.conversation_id,
            followup_to_task_id=None,
            status="timed_out",
        )

    async def lead_sends(self, correlation_id: str, content) -> None:
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=content,
                msg_meta=self.meta(correlation_id),
                msg_header=MessageHeader(from_agent_id=self.config.manager.manager_agent_id),
            )
        )

    async def person_says(self, text: str):
        return await ChannelBridge(self.gateway.application).handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=TELEGRAM_ID,
                chat_id=TELEGRAM_ID,
                session_key=self.session.session_key,
                text=text,
            )
        )

    def open_question_ids(self) -> list[str]:
        return [
            question.task_query_id
            for question in self.store.list_open_pending_queries(self.session.session_key)
        ]

    def finish_each_answered_task(self) -> None:
        async def finish(envelope: MessageEnvelope) -> None:
            task_id = envelope.msg_content.parent_task_id
            await self.lead_sends(
                envelope.msg_meta.msg_correlation_id, _finished(task_id, f"Done with {task_id}")
            )

        self.lead.on_answer = finish


def _question(parent_task_id: str, text: str) -> TaskQuery:
    return TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id=parent_task_id,
        message=text,
    )


def _finished(task_id: str, text: str) -> Task:
    return Task(
        task_id=task_id,
        task_type=MessageMetaType.RESPONSE,
        answer=TaskAnswer(answer=text),
    )


REPORT_QUESTION = "Which folder holds the report?"
TRIP_QUESTION = "Which week should the trip be?"


async def _two_requests_ask(chat: _Chat) -> tuple[TaskQuery, TaskQuery]:
    """Two requests in one chat ask, one after the other, after their waits ended."""

    chat.record_request("corr_report", "Summarize the sales report")
    chat.record_request("corr_trip", "Plan the team trip")
    first = _question("task_report", REPORT_QUESTION)
    second = _question("task_trip", TRIP_QUESTION)
    await chat.lead_sends("corr_report", first)
    await chat.lead_sends("corr_trip", second)
    return first, second


@pytest.mark.asyncio
async def test_a_second_question_waits_and_each_answer_reaches_its_own_question(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()

    replies = await chat.person_says("The shared reports folder")

    answers = chat.lead.answers()
    assert [answer.task_query_id for answer in answers] == [first.task_query_id]
    assert answers[0].parent_task_id == "task_report"
    assert answers[0].message == "The shared reports folder"
    assert chat.lead.durable[0].msg_meta.msg_correlation_id == "corr_report"
    assert replies[0].text == "Done with task_report"

    # The person saw the first question, then a note that another one waits,
    # and the second question only once the first was answered.
    assert [event.text for event in chat.shown[:3]] == [
        REPORT_QUESTION,
        QUESTION_WAITS_IN_LINE_TEXT,
        TRIP_QUESTION,
    ]
    assert chat.shown[2].correlation_id == "corr_trip"
    assert chat.shown[2].message_type == "task_query"

    replies = await chat.person_says("The second week of June")
    assert chat.lead.answer_ids() == [first.task_query_id, second.task_query_id]
    assert chat.lead.durable[-1].msg_meta.msg_correlation_id == "corr_trip"
    assert replies[0].text == "Done with task_trip"
    assert chat.open_question_ids() == []


@pytest.mark.asyncio
async def test_a_second_answer_to_the_same_question_is_not_sent(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    application = chat.gateway.application
    # Two messages found the same question before either was answered, as
    # with a double send.
    question_id = await application.waiting_question_id(chat.session.session_key)
    assert question_id == first.task_query_id

    await application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=question_id,
        response_text="The shared reports folder",
    )
    duplicate = await application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=question_id,
        response_text="The shared reports folder",
    )

    assert duplicate.text == ALREADY_ANSWERED_TEXT
    assert chat.lead.answer_ids() == [first.task_query_id]
    # The second question was shown once, after the first answer.
    assert [event.text for event in chat.shown].count(TRIP_QUESTION) == 1
    assert chat.open_question_ids() == [second.task_query_id]


@pytest.mark.asyncio
async def test_a_question_arriving_again_after_its_answer_is_not_asked_again(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    chat.record_request("corr_report", "Summarize the sales report")
    question = _question("task_report", REPORT_QUESTION)
    await chat.lead_sends("corr_report", question)
    chat.finish_each_answered_task()
    await chat.person_says("The shared reports folder")
    shown_before = len(chat.shown)

    # A redelivered copy of the answered question.
    await chat.lead_sends("corr_report", question)

    assert len(chat.shown) == shown_before
    assert chat.open_question_ids() == []
    assert await chat.gateway.application.waiting_question_id(chat.session.session_key) is None
    assert chat.lead.answer_ids() == [question.task_query_id]


@pytest.mark.asyncio
async def test_when_a_task_finishes_its_question_ends_and_the_next_is_shown(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)

    # The report request stops waiting (for example its own wait limit) and
    # sends its final reply without an answer.
    await chat.lead_sends("corr_report", _finished("task_report", "Stopped waiting."))

    assert chat.store.get_pending_query(first.task_query_id).status == "request_ended"
    assert [event.text for event in chat.shown[-2:]] == ["Stopped waiting.", TRIP_QUESTION]
    chat.finish_each_answered_task()
    await chat.person_says("The second week of June")
    assert chat.lead.answer_ids() == [second.task_query_id]


@pytest.mark.asyncio
async def test_a_reply_to_a_question_whose_task_is_gone_shows_the_next_question(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    chat.lead.unknown_tasks.add("task_report")  # the lead lost the task

    replies = await chat.person_says("The shared reports folder")

    # The reply was meant for the question the person saw; it is not sent to
    # the other request, whose question is shown instead.
    assert chat.lead.answer_ids() == []
    assert replies[0].text == f"{QUESTION_WAITING_INTRO_TEXT}\n\n{TRIP_QUESTION}"
    assert chat.store.get_pending_query(first.task_query_id).status == "request_ended"
    chat.finish_each_answered_task()
    await chat.person_says("The second week of June")
    assert chat.lead.answer_ids() == [second.task_query_id]


@pytest.mark.asyncio
async def test_an_expired_question_gives_way_to_the_next(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    with chat.store._repository._connect() as conn:  # the first was asked long ago
        conn.execute(
            "UPDATE gateway_questions SET created_at = ? WHERE task_query_id = ?",
            ("2000-01-01T00:00:00+00:00", first.task_query_id),
        )
        conn.commit()

    replies = await chat.person_says("The shared reports folder")

    assert chat.lead.answer_ids() == []
    assert replies[0].text.endswith(TRIP_QUESTION)
    assert chat.store.get_pending_query(first.task_query_id).status == "expired"
    assert chat.open_question_ids() == [second.task_query_id]


@pytest.mark.asyncio
async def test_a_question_that_ended_after_the_check_is_not_answered(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    chat.record_request("corr_report", "Summarize the sales report")
    question = _question("task_report", REPORT_QUESTION)
    await chat.lead_sends("corr_report", question)
    application = chat.gateway.application
    question_id = await application.waiting_question_id(chat.session.session_key)
    await chat.lead_sends("corr_report", _finished("task_report", "Stopped waiting."))

    response = await application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=question_id,
        response_text="The shared reports folder",
    )

    assert response.text == NO_LONGER_WAITING_TEXT
    assert chat.lead.answer_ids() == []


@pytest.mark.asyncio
async def test_waiting_questions_survive_a_gateway_restart(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)

    chat.restart()
    chat.finish_each_answered_task()
    await chat.person_says("The shared reports folder")
    await chat.person_says("The second week of June")

    assert chat.lead.answer_ids() == [first.task_query_id, second.task_query_id]
    assert [answer.message for answer in chat.lead.answers()] == [
        "The shared reports folder",
        "The second week of June",
    ]


@pytest.mark.asyncio
async def test_a_newer_question_of_the_same_task_takes_its_place(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    rephrased = _question("task_report", "Which shared folder holds the report?")

    await chat.lead_sends("corr_report", rephrased)

    # The task waits only for its latest question, which keeps the first place.
    assert chat.shown[-1].text == "Which shared folder holds the report?"
    assert chat.open_question_ids() == [rephrased.task_query_id, second.task_query_id]
    chat.finish_each_answered_task()
    await chat.person_says("The shared reports folder")
    assert chat.lead.answer_ids() == [rephrased.task_query_id]


@pytest.mark.asyncio
async def test_an_answer_the_gateway_could_not_send_leaves_the_question_open(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)

    async def fail(_envelope: MessageEnvelope) -> None:
        raise RuntimeError("Publication failed")

    chat.lead.on_answer = fail
    with pytest.raises(RuntimeError):
        await chat.person_says("The shared reports folder")

    assert chat.open_question_ids() == [first.task_query_id, second.task_query_id]
    # The second question is still not shown: the first is still the one asked.
    assert [event.text for event in chat.shown].count(TRIP_QUESTION) == 0


@pytest.mark.asyncio
async def test_cancelling_before_answer_publication_keeps_the_question_open(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    entered = asyncio.Event()
    publish = chat.lead.publish_durable

    async def wait_before_send(envelope, *, routing_key):
        entered.set()
        await asyncio.Event().wait()

    chat.lead.publish_durable = wait_before_send
    answer = asyncio.create_task(chat.gateway.application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=first.task_query_id,
        response_text="The shared reports folder",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 10)
    finally:
        answer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await answer
        chat.lead.publish_durable = publish

    assert chat.lead.answer_ids() == []
    assert chat.open_question_ids() == [first.task_query_id, second.task_query_id]
    assert chat.store.get_pending_query(second.task_query_id).was_shown is False
    chat.finish_each_answered_task()
    await chat.person_says("The shared reports folder")
    assert chat.lead.answer_ids() == [first.task_query_id]


@pytest.mark.asyncio
async def test_failed_reply_channel_registration_keeps_the_answer_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)

    def refuse(*args, **kwargs):
        raise RuntimeError("Reply wait could not be registered")

    with monkeypatch.context() as patch:
        patch.setattr(chat.gateway._manager_client, "register_response_channel", refuse)
        with pytest.raises(RuntimeError, match="could not be registered"):
            await chat.gateway.application.reply_to_pending_query(
                session_key=chat.session.session_key,
                task_query_id=first.task_query_id,
                response_text="The shared reports folder",
            )
    assert chat.lead.answer_ids() == []
    assert chat.open_question_ids() == [first.task_query_id, second.task_query_id]
    chat.finish_each_answered_task()
    await chat.person_says("The shared reports folder")
    assert chat.lead.answer_ids() == [first.task_query_id]


@pytest.mark.asyncio
async def test_cancelling_after_answer_publication_does_not_send_the_answer_twice(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, _ = await _two_requests_ask(chat)
    sent = asyncio.Event()

    async def answer_sent(envelope):
        sent.set()

    chat.lead.on_answer = answer_sent
    answer = asyncio.create_task(chat.gateway.application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=first.task_query_id,
        response_text="The shared reports folder",
    ))
    try:
        await asyncio.wait_for(sent.wait(), 10)
    finally:
        answer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await answer
    response = await chat.gateway.application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=first.task_query_id,
        response_text="The shared reports folder",
    )
    assert response.text == ALREADY_ANSWERED_TEXT
    assert chat.lead.answer_ids() == [first.task_query_id]
    assert chat.store.get_pending_query(first.task_query_id).status == "answered"


@pytest.mark.asyncio
async def test_questions_sharing_a_schedule_correlation_have_separate_reply_waits(tmp_path: Path) -> None:
    """Two schedule actions created in one request keep that request's correlation."""
    chat = _Chat(tmp_path)
    chat.record_request("corr_schedules", "Two recurring reports")
    first = _question("task_first_schedule", REPORT_QUESTION)
    second = _question("task_second_schedule", TRIP_QUESTION)
    await chat.lead_sends("corr_schedules", first)
    await chat.lead_sends("corr_schedules", second)
    sent = [asyncio.Event(), asyncio.Event()]

    async def answer_sent(envelope):
        index = 0 if envelope.msg_content.task_query_id == first.task_query_id else 1
        sent[index].set()

    chat.lead.on_answer = answer_sent
    replies = []
    try:
        async with asyncio.timeout(10):
            for index, question in enumerate((first, second)):
                replies.append(asyncio.create_task(chat.gateway.application.reply_to_pending_query(
                    session_key=chat.session.session_key,
                    task_query_id=question.task_query_id,
                    response_text=f"Answer {index}",
                )))
                await sent[index].wait()
                while not chat.store.get_pending_query(second.task_query_id).was_shown:
                    await asyncio.sleep(0)
            await chat.lead_sends("corr_schedules", _finished(second.parent_task_id, "Second finished"))
            assert (await replies[1]).text == "Second finished"
            assert not replies[0].done()
            await chat.lead_sends("corr_schedules", _finished(first.parent_task_id, "First finished"))
            assert (await replies[0]).text == "First finished"
    finally:
        for reply in replies:
            reply.cancel()
        await asyncio.gather(*replies, return_exceptions=True)
    assert chat.lead.answer_ids() == [first.task_query_id, second.task_query_id]
    assert chat.open_question_ids() == []


@pytest.mark.asyncio
async def test_a_sibling_question_answer_keeps_the_original_request_and_its_steering_state(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    chat.store.record_request(
        correlation_id="corr_shared", session_key=chat.session.session_key,
        request_kind="interactive", request_text="Original request",
        conversation_id=chat.session.conversation_id, followup_to_task_id=None,
        active_task_id="task_original", run_state="running", status="waiting_for_steering",
    )
    question = _question("task_scheduled", REPORT_QUESTION)
    await chat.lead_sends("corr_shared", question)
    before = chat.store.get_request("corr_shared")
    chat.finish_each_answered_task()
    result = await chat.person_says("The shared reports folder")

    assert result[0].text == "Done with task_scheduled"
    assert chat.store.get_request("corr_shared") == before
    assert chat.lead.answer_ids() == [question.task_query_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_a_question_that_could_not_be_shown_still_waits_to_be_shown(
    tmp_path: Path, failure_type
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    questions = chat.gateway._pending_questions
    assert questions.claim_answer(first.task_query_id)
    emit = chat.gateway._event_output.emit

    async def fail(_event):
        raise failure_type("The chat adapter could not deliver the question")

    chat.gateway._event_output.emit = fail
    with pytest.raises(failure_type, match="could not deliver"):
        await questions.show_next(chat.session.session_key)
    assert chat.store.get_pending_query(second.task_query_id).was_shown is False

    chat.gateway._event_output.emit = emit
    await questions.show_next(chat.session.session_key)
    assert chat.shown[-1].text == TRIP_QUESTION
    assert chat.store.get_pending_query(second.task_query_id).was_shown is True


@pytest.mark.asyncio
async def test_web_chat_answers_questions_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _Chat(tmp_path, channel="web")
    config = chat.config
    config.network.public_origin = "http://127.0.0.1"
    config.interactive_apps.internal_website.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    app = InternalWebsiteInteractiveApp(config, gateway=chat.gateway, bridge=SimpleNamespace())

    async with TestClient(TestServer(app.build_app())) as client:
        auth = await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})
        headers = {"Cookie": f"ua_web_chat_session={auth.cookies['ua_web_chat_session'].value}"}
        created = await (await client.post("/api/web-chat/sessions", headers=headers)).json()
        session_key = created["session"]["session_key"]
        chat.session = chat.store.get_session(session_key)
        first, second = await _two_requests_ask(chat)
        chat.finish_each_answered_task()

        async def send(text: str, request_id: str) -> dict:
            response = await client.post(
                "/api/web-chat/chat",
                headers=headers,
                json={"message": text, "session_key": session_key, "client_request_id": request_id},
            )
            assert response.status == 200
            return await response.json()

        answered = await send("The shared reports folder", "answer_1")
        retried = await send("The shared reports folder", "answer_1")  # browser retry
        transcript = await (
            await client.get(f"/api/web-chat/sessions/{session_key}/messages", headers=headers)
        ).json()
        await send("The second week of June", "answer_2")

    assert chat.lead.answer_ids() == [first.task_query_id, second.task_query_id]
    assert answered["messages"][-1]["content"] == "Done with task_report"
    assert retried["messages"][-1]["content"] == "Done with task_report"
    contents = [message["content"] for message in transcript["messages"]]
    assert contents.index(REPORT_QUESTION) < contents.index(QUESTION_WAITS_IN_LINE_TEXT)
    assert contents.index("The shared reports folder") < contents.index(TRIP_QUESTION)
