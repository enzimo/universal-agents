"""Files a person sends with the answer to a question reach the request, through the real gateway.

A team lead's question (a ``TaskQuery``) waits in the person's chat, and the
person answers it with a file. The gateway sends the file with the answer
(``TaskQuery.attachments``) under a context it signs for that answer, bound
to the question and the file (``access_control.ownership.answer_context_id``).
The team lead keeps the file with the answer's text only when that context
vouches for it, adds it to the request's conversation turn and history,
names it by id under the answer in the model's prompt, and it stays usable
after a restart and in a follow-up turn.

The end-to-end tests run the real gateway (Web Chat, the chat bridge, its
SQLite store and upload storage) against a real team lead
(``UniversalAgent`` with its admission and ``TaskQuery`` handlers, SQLite
task and conversation stores, its context engine and the attachment tools);
envelopes pass through the NATS encoding between them. Covers:

- Web Chat and the chat bridge send an answer with files (no longer refused);
- the lead keeps them: on the turn, among the task's files, named by id in
  the prompt, after a restart and in a follow-up turn;
- an answer to a question a plan step or an isolated recovery task asked
  (each a task with its own conversation id and no turn) keeps its files on
  the step and on the request it runs for, and a follow-up request opens
  them; a request working for someone else gets none (logged);
- an answer deferred while the task's turn runs keeps its files only when it
  is handled from the runtime mailbox, once;
- refused: an answer for a task the lead no longer holds, a context that
  does not vouch for these files (a worker's copy of the request's context,
  or the answer's own context with another file), files on a question an
  agent asks, and a delegated task's answers;
- an answer that does not parse as a pause decision keeps its files with its
  text (the question is asked again);
- when the gateway does not send a message as the answer, its reply says the
  files were not sent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from access_control.ownership import answer_context_id, request_context_id
from assistant_gateway.application.pending_questions import (
    ALREADY_ANSWERED_TEXT,
    ANSWER_FILES_NOT_SENT_TEXT,
    QUESTION_WAITING_INTRO_TEXT,
)
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    store_inbound_attachment_bytes_with_policy,
)
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.context_engine import ContextAssemblyRequest
from custom_agents.conversation_attachments import QUESTION_ANSWER_MESSAGE_TYPE
from custom_agents.task_context_runtime import context_verifier
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.envelope_v1 import CommsMessage, MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskStatus
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskQueryDecision,
    TaskQueryReason,
)
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, forget_task_context_keys, use_gateway_key
from tests.task_work_support import use_agent
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, TEAM, _admit, _answer, _lead
from tests.test_gateway_parallel_threads import WEB_CHAT_TOKEN, _send, _web_chat_thread
from tests.test_gateway_question_queue import _Chat, _two_requests_ask
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp
from tests.test_turn_files_recording import (
    ATTACHMENTS_LOGGER,
    TASK,
    _files_entries,
    _messages,
    _outside,
    _own,
    _restarted,
    _turn_files,
    _upload,
)

pytestmark = pytest.mark.integration

LEAD_ID = "lead"
QUESTION = "Which shopping list should I use?"


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch):
    # The gateway signs with the test gateway key; agents trust it.
    use_gateway_key(monkeypatch)
    yield
    forget_task_context_keys()


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch):
    # Each test plays the model turn itself (``_ModelTurns``).
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)
    monkeypatch.setattr(task_steering_runtime, "schedule_pending_work", lambda _agent: None)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


class _ModelTurns:
    """Stands in for the lead's model: records what each turn would send, then does nothing.

    A task turn's assembled prompt goes to ``prompts``; a question an agent
    asked (the lead answers it with a model turn) goes to ``questions``.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.prompts: list[Any] = []
        self.questions: list[TaskQuery] = []

    async def __call__(self, *, msg_content: Any, msg_header: MessageHeader, msg_meta: MessageMeta) -> Any:
        if isinstance(msg_content, TaskQuery):
            self.questions.append(msg_content)
            return msg_content
        use_agent(self.agent, msg_content.task_id)
        self.prompts.append(_prompt(self.agent, msg_content, msg_header, msg_meta))
        return None


def _prompt(agent: Any, task: Task, header: MessageHeader, meta: MessageMeta) -> Any:
    return agent._context_engine.assemble(
        ContextAssemblyRequest(
            incoming_content=task,
            incoming_header=header,
            incoming_meta=meta,
            identity_meta=meta,
            execution_task_id=task.task_id,
            incoming_message_json=CommsMessage(msg_content=task, msg_meta=meta).model_dump_json(),
        )
    )


def _sections(assembly: Any) -> dict[str, str]:
    return {section.name: section.text for section in assembly.sections}


def _turn_files_of(agent: Any, task_id: str) -> list[str]:
    return [item["attachment_id"] for item in agent._conversation_store.get_turn_by_task_id(task_id)["attachments"]]


# --- End to end: Web Chat to the team lead ---------------------------------------------------------


class _TeamLead:
    """The gateway's manager transport, delivering each envelope as NATS would to a real team lead.

    A new request gets one question back (asked the way the lead records its
    own questions); an answer and any later request are handled and finished.
    """

    connected = True

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.gateway: UserGateway | None = None
        self.durable: list[MessageEnvelope] = []
        self.ask_next_request = True

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        envelope = decode_envelope(encode_envelope(envelope))
        self.durable.append(envelope)
        content = envelope.msg_content
        if isinstance(content, Task):
            await task_message_runtime._handle_task_request(
                self.agent, content, envelope.msg_header, envelope.msg_meta
            )
            if self.ask_next_request:
                self.ask_next_request = False
                question = TaskQuery(
                    query_type=MessageMetaType.REQUEST, parent_task_id=content.task_id, message=QUESTION
                )
                self.agent.task_manager.add_task_query(content.task_id, question, agent_name="Lead", agent_id=LEAD_ID)
                await self._reply(envelope, question)
                return
            await self._reply(envelope, Task(task_id=content.task_id, task_type=MessageMetaType.RESPONSE, answer="Done."))
            return
        await task_message_runtime._handle_task_query_message(
            self.agent, content, envelope.msg_header, envelope.msg_meta
        )
        await self._reply(
            envelope, Task(task_id=content.parent_task_id, task_type=MessageMetaType.RESPONSE, answer="Done.")
        )

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        # Questions' tasks are checked with a status probe; the lead knows them.
        request = envelope.msg_content
        if not isinstance(request, TaskStatus) or self.gateway is None:
            return
        await self.gateway._manager_client.get_control_channel(envelope.msg_meta.msg_correlation_id).put(
            MessageEnvelope(
                msg_content=request.model_copy(
                    update={"status_type": MessageMetaType.RESPONSE, "is_known_task": True, "responder_agent_id": LEAD_ID}
                ),
                msg_header=MessageHeader(from_agent_id=LEAD_ID),
                msg_meta=envelope.msg_meta,
            )
        )

    async def _reply(self, envelope: MessageEnvelope, content: Any) -> None:
        assert self.gateway is not None
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=content,
                msg_header=MessageHeader(from_agent_id=LEAD_ID, agent_signature=""),
                msg_meta=envelope.msg_meta.model_copy(
                    update={"requester_type": RequesterType.USER_REQUESTER, "is_response_requested": False}
                ),
            )
        )


def _web_chat_gateway(tmp_path: Path, layout: InstanceLayout, lead: _TeamLead, monkeypatch) -> UserGateway:
    config = GatewayInstanceConfig.create(
        instance_name="answer_files",
        instance_root=str(layout.root),
        team_id=TEAM,
        timezone="UTC",
        manager_agent_id=LEAD_ID,
        response_queue_name="test_team_gateway",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    config.network.public_origin = "http://127.0.0.1"
    config.interactive_apps.internal_website.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", WEB_CHAT_TOKEN)
    gateway = UserGateway(
        config, output_sink=None, state_store=GatewayStateStore(str(tmp_path / "gateway.db")), manager_transport=lead
    )
    lead.gateway = gateway
    return gateway


@pytest.mark.asyncio
async def test_a_web_chat_answer_with_a_file_reaches_the_request_named_by_id_and_stays_usable(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _lead(tmp_path, layout)
    turns = _ModelTurns(agent)
    object.__setattr__(agent, "process_llm_interaction", turns)
    lead = _TeamLead(agent)
    gateway = _web_chat_gateway(tmp_path, layout, lead, monkeypatch)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())

    async with TestClient(TestServer(app.build_app())) as client:
        thread = await _web_chat_thread(client)
        asked = await _send(client, thread, "Plan dinner for four.", request_id="request")
        assert asked.status == 200
        assert QUESTION in (await asked.json())["messages"][-1]["content"]
        answered = await _send(client, thread, "Use this list.", request_id="answer", with_file=True)
        assert answered.status == 200, await answered.text()
        assert (await answered.json())["messages"][-1]["content"] == "Done."
        transcript = await (await client.get(f"/api/web-chat/sessions/{thread['session_key']}/messages")).json()

    request_envelope, answer_envelope = lead.durable
    task_id = request_envelope.msg_content.task_id
    answer = answer_envelope.msg_content
    [notes] = answer.attachments
    assert (answer.query_type, answer.parent_task_id, answer.message) == (MessageMetaType.RESPONSE, task_id, "Use this list.")
    # The answer's own context vouches for exactly this answer and file, as the lead derives it.
    signed = context_verifier().verify(answer_envelope.msg_meta.gateway_context.task_context)
    assert (signed.root_task_id, signed.conversation_id) == (task_id, thread["conversation_id"])
    assert signed.context_id == answer_context_id(answer)
    assert signed.context_id != request_context_id(request_envelope.msg_content)
    # The person's answer shows its file in Web Chat.
    [shown] = [message for message in transcript["messages"] if message["content"] == "Use this list."]
    assert [item["attachment_id"] for item in shown["attachments"]] == [notes.attachment_id]

    # The lead kept the file: on the turn, among the task's files, and on the stored answer.
    assert notes.attachment_id in _turn_files_of(agent, task_id)
    [entry] = _files_entries(agent.task_manager, task_id)
    assert (entry.details["message_id"], entry.details["message_type"]) == (answer.task_query_id, QUESTION_ANSWER_MESSAGE_TYPE)
    assert notes.attachment_id in _own(agent.task_manager, agent._conversation_store, task_id)
    stored = agent.task_manager.get_task_queries(task_id)[-1]
    assert (stored.query_type, stored.attachments) == (MessageMetaType.RESPONSE, [notes])
    # The model's turn names it by id under the answer, and lists it, never by path.
    [assembly] = turns.prompts
    history = _sections(assembly)["task_history"]
    assert "Use this list." in history
    assert f"`{notes.attachment_id}` notes.txt (text/plain, document, 5 bytes)" in history
    assert notes.attachment_id in _sections(assembly)["conversation_attachments"]
    assert str(layout.uploads_dir) not in assembly.prompt

    # After a restart the file is still the task's own.
    manager, store = _restarted(agent, tmp_path)
    assert notes.attachment_id in _own(manager, store, task_id)

    # A follow-up turn in the same chat, in a restarted lead, lists and opens it by id.
    await _answer(agent, task_id, "Here is the plan.")
    follow_up_agent = _lead(tmp_path, layout)
    lead.agent = follow_up_agent
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        await client.post("/api/web-chat/auth", json={"token": WEB_CHAT_TOKEN})
        follow_up = await _send(client, thread, "What was on that list?", request_id="follow_up")
        assert follow_up.status == 200
    follow_up_id = lead.durable[-1].msg_content.task_id
    info = follow_up_agent.task_manager.get_task_info(follow_up_id)
    use_agent(follow_up_agent, follow_up_id)
    listed = _sections(_prompt(follow_up_agent, info.task, info.header, info.meta))["conversation_attachments"]
    assert f"`{notes.attachment_id}` notes.txt" in listed
    opened = inspect_user_attachment._tool_func(attachment_id=notes.attachment_id)
    assert opened["status"] == "success", opened
    assert str(layout.uploads_dir) not in opened["content"][0]["text"]


# --- The chat bridge ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_chat_bridge_sends_the_files_of_an_answer_with_it(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, _second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    photo = _chat_upload(chat, "pan.png")

    await chat.person_says_with(photo, text="This pan.")

    [answer] = chat.lead.answers()
    assert (answer.task_query_id, answer.message, answer.attachments) == (first.task_query_id, "This pan.", [photo])
    envelope = chat.lead.durable[-1]
    signed = context_verifier().verify(envelope.msg_meta.gateway_context.task_context)
    assert (signed.root_task_id, signed.conversation_id) == (first.parent_task_id, chat.session.conversation_id)
    assert signed.context_id == answer_context_id(decode_envelope(encode_envelope(envelope)).msg_content)


@pytest.mark.asyncio
async def test_files_alone_answer_a_question_in_the_chat_bridge(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, _second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    photo = _chat_upload(chat, "pan.png")

    await chat.person_says_with(photo, text="")

    [answer] = chat.lead.answers()
    assert (answer.task_query_id, answer.message, answer.attachments) == (
        first.task_query_id,
        "User sent attachment(s).",
        [photo],
    )


def _chat_upload(chat: _Chat, name: str) -> UserTaskAttachment:
    """A file the chat's adapter stored for the person's message."""

    return store_inbound_attachment_bytes_with_policy(
        data=b"\x89PNG\r\n\x1a\n" + name.encode(),
        filename=name,
        content_type="image/png",
        policy=InboundAttachmentStoragePolicy(cache_dir=chat.tmp_path / "uploads"),
        session_key=chat.session.session_key,
        conversation_id=chat.session.conversation_id,
        origin="telegram",
    ).attachment


async def _person_says_with(self: _Chat, attachment: UserTaskAttachment, *, text: str):
    return await ChannelBridge(self.gateway.application).handle_inbound(
        ChannelInboundMessage(
            channel_name="telegram",
            user_id=self.session.user_id,
            chat_id=self.session.chat_id,
            session_key=self.session.session_key,
            text=text,
            attachments=[attachment],
        )
    )


_Chat.person_says_with = _person_says_with  # type: ignore[attr-defined]


# --- When the gateway does not send a message as the answer ---------------------------------------


@pytest.mark.asyncio
async def test_an_answer_that_is_not_sent_says_its_files_were_not_sent_either(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, _second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    application = chat.gateway.application
    photo = _chat_upload(chat, "pan.png")
    await application.reply_to_pending_query(
        session_key=chat.session.session_key, task_query_id=first.task_query_id, response_text="This pan."
    )

    # The question already has an answer.
    duplicate = await application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=first.task_query_id,
        response_text="And this one.",
        attachments=[photo],
    )
    assert duplicate.text == f"{ANSWER_FILES_NOT_SENT_TEXT}\n\n{ALREADY_ANSWERED_TEXT}"
    assert [answer.attachments for answer in chat.lead.answers()] == [[]]


@pytest.mark.asyncio
async def test_a_message_with_files_for_a_question_not_yet_shown_says_its_files_were_not_sent(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    _first, second = await _two_requests_ask(chat)
    # The second question waits in line: the person has not seen it yet.
    assert chat.store.get_pending_query(second.task_query_id).was_shown is False

    reply = await chat.gateway.application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=second.task_query_id,
        response_text="The second week of June.",
        attachments=[_chat_upload(chat, "calendar.png")],
    )

    assert reply.text.startswith(f"{ANSWER_FILES_NOT_SENT_TEXT}\n\n{QUESTION_WAITING_INTRO_TEXT}")
    assert chat.lead.answers() == []


@pytest.mark.asyncio
async def test_an_answer_with_files_while_the_team_is_unavailable_says_its_files_were_not_sent(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, _second = await _two_requests_ask(chat)
    await chat.gateway._manager_client.mark_manager_unavailable(reason="test: the team lead stopped")

    reply = await chat.gateway.application.reply_to_pending_query(
        session_key=chat.session.session_key,
        task_query_id=first.task_query_id,
        response_text="This pan.",
        attachments=[_chat_upload(chat, "pan.png")],
    )

    assert reply.text.startswith(f"{ANSWER_FILES_NOT_SENT_TEXT}\n\n")
    assert chat.lead.answers() == []
    assert first.task_query_id in chat.open_question_ids()


# --- The team lead's decision on an answer's files -----------------------------------------------


@pytest_asyncio.fixture
async def asking_lead(tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch):
    """Ann's team lead with one admitted request (TASK) waiting on its question."""

    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner.", attachments=[])
    agent.task_manager.add_task_query(TASK, _asked(), agent_name="Lead", agent_id=LEAD_ID)
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    return agent


def _asked(**fields: Any) -> TaskQuery:
    return TaskQuery(
        task_query_id="task_query_list",
        query_type=MessageMetaType.REQUEST,
        parent_task_id=TASK,
        message=QUESTION,
        **fields,
    )


def _answer_to(files: list[UserTaskAttachment], *, text: str = "Use this list.", task_id: str = TASK) -> TaskQuery:
    return TaskQuery(
        task_query_id="task_query_list",
        query_type=MessageMetaType.RESPONSE,
        parent_task_id=task_id,
        message=text,
        attachments=files,
    )


def _answer_meta(signed_for: TaskQuery, *, task_id: str = TASK) -> MessageMeta:
    """Message meta as the gateway sends an answer: a context bound to ``signed_for`` and its files."""

    signed = context_signer().issue(
        team_id=TEAM,
        person_id=ANN,
        person_roles=("member",),
        root_task_id=task_id,
        conversation_id=CONVERSATION,
        context_id=answer_context_id(signed_for),
    )
    return MessageMeta(
        conversation_id=CONVERSATION,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=ANN, task_context=signed),
    )


async def _deliver(agent: Any, query: TaskQuery, meta: MessageMeta, **kwargs: Any) -> None:
    await task_message_runtime._handle_task_query_message(
        agent, query, MessageHeader(from_agent_id="gateway"), meta, **kwargs
    )


@pytest.mark.asyncio
async def test_a_deferred_answer_keeps_its_files_once_when_handled_from_the_runtime_mailbox(
    asking_lead, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = asking_lead._runtime_coordinator()
    # The task's turn holds the runtime lease, so the answer waits in the mailbox.
    monkeypatch.setattr(coordinator, "should_defer_for_active_lease", lambda **_kw: (True, SimpleNamespace(lease_id="lease_1")))
    monkeypatch.setattr(coordinator, "enforced", lambda: True)
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes])

    await _deliver(asking_lead, answer, _answer_meta(answer))

    assert _files_entries(asking_lead.task_manager) == []
    assert notes.attachment_id not in _turn_files(asking_lead)
    [event] = coordinator.runtime.list_deferred_runtime_events(TASK)

    # Handled from the mailbox (header, meta and answer read back from storage), then delivered again.
    await coordinator._process_mailbox_event(event)
    await coordinator._process_mailbox_event(event)

    assert _turn_files(asking_lead).count(notes.attachment_id) == 1
    assert _own(asking_lead.task_manager, asking_lead._conversation_store).count(notes.attachment_id) == 1
    assert asking_lead.task_manager.get_task_queries(TASK)[-1].attachments == [notes]


@pytest.mark.asyncio
async def test_records_outside_the_upload_folders_are_not_kept_with_an_answer(
    asking_lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes, _outside(layout)])

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _deliver(asking_lead, answer, _answer_meta(answer))

    assert asking_lead.task_manager.get_task_queries(TASK)[-1].attachments == [notes]
    assert _own(asking_lead.task_manager, asking_lead._conversation_store) == [notes.attachment_id]
    assert _messages(caplog, "attachment_outside_upload_folders")


@pytest.mark.asyncio
async def test_an_answer_whose_files_the_gateway_did_not_vouch_for_keeps_none(
    asking_lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    notes = _upload(layout, "list.png")
    bobs = _upload(layout, "bob.png", session_key="web:bob", conversation_id="conv_bob")
    genuine = _answer_to([notes])
    # The answer's own context, sent again with another person's upload.
    swapped = _answer_to([bobs])
    # A worker's copy of the request's context (delegation copies it into every subtask).
    request_meta = asking_lead.task_manager.get_task_info(TASK).meta

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _deliver(asking_lead, swapped, _answer_meta(genuine))
        await _deliver(asking_lead, swapped, request_meta)

    answers = [query for query in asking_lead.task_manager.get_task_queries(TASK) if query.query_type == MessageMetaType.RESPONSE]
    assert [answer.attachments for answer in answers] == [[], []]
    assert [answer.message for answer in answers] == ["Use this list.", "Use this list."]
    assert _files_entries(asking_lead.task_manager) == []
    assert bobs.attachment_id not in _turn_files(asking_lead)
    refused = _messages(caplog, "sent_file_refused")
    assert len(refused) == 2 and all("reason=context_not_for_message" in line for line in refused)


@pytest.mark.asyncio
async def test_an_answer_for_a_task_the_lead_no_longer_holds_keeps_no_files(
    asking_lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], task_id="task_ended")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _deliver(asking_lead, answer, _answer_meta(answer, task_id="task_ended"))

    assert notes.attachment_id not in [item.attachment.attachment_id for item in asking_lead._conversation_store.list_turn_attachments(CONVERSATION)]
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=task_not_found attachment_id={notes.attachment_id} task_id=task_ended" in refused


@pytest.mark.asyncio
async def test_a_question_an_agent_asks_brings_no_files(
    asking_lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    notes = _upload(layout, "list.png")
    asked = _asked(attachments=[notes]).model_copy(update={"task_query_id": "task_query_worker"})

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _deliver(asking_lead, asked, _answer_meta(asked))

    # Neither the stored question nor the model's turn that answers it has the file.
    assert asking_lead.task_manager.get_task_queries(TASK)[-1].attachments == []
    [seen] = asking_lead.process_llm_interaction.questions
    assert seen.attachments == []
    assert _files_entries(asking_lead.task_manager) == []
    [refused] = _messages(caplog, "sent_file_refused")
    assert "reason=not_an_answer" in refused


@pytest.mark.parametrize(
    "case",
    [
        # Delegated work that is not an escalation, with no request held here.
        "not_an_escalation",
        # An escalation held by an agent that is not the team lead: only the lead takes escalations.
        "not_the_team_lead",
    ],
)
@pytest.mark.asyncio
async def test_a_delegated_task_keeps_no_files_from_an_answer(
    case: str, tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _lead(tmp_path, layout)
    escalation = None
    if case == "not_the_team_lead":
        worker.is_team_lead = False
        escalation = ManagerEscalationContext(escalation_type="needs_user_input")
    worker.task_manager.add_task(
        Task(task_id="task_child", parent_task_id="task_parent", task_type=MessageMetaType.REQUEST, problem_statement="Parse it."),
        MessageHeader(from_agent_id=LEAD_ID),
        MessageMeta(conversation_id=CONVERSATION, manager_escalation=escalation),
        owner_person_id=ANN,
    )
    object.__setattr__(worker, "process_llm_interaction", _ModelTurns(worker))
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], task_id="task_child")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _deliver(worker, answer, _answer_meta(answer, task_id="task_child"))

    assert worker.task_manager.get_task_queries("task_child")[-1].attachments == []
    assert _files_entries(worker.task_manager, "task_child") == []
    [refused] = _messages(caplog, "sent_file_refused")
    assert "reason=delegated_task" in refused
    # The gateway did vouch for the file, so the warning says why it was not kept instead.
    assert "is delegated work that keeps no files from this message" in refused
    assert "gateway" not in refused


@pytest.mark.asyncio
async def test_a_pause_answer_asked_again_keeps_its_files_with_its_text(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner.", attachments=[])
    pause = _asked(
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        allowed_decisions=[TaskQueryDecision.RESUME, TaskQueryDecision.CANCEL],
    )
    agent.task_manager.add_task_query(TASK, pause, agent_name="Lead", agent_id=LEAD_ID)
    turns = _ModelTurns(agent)
    object.__setattr__(agent, "process_llm_interaction", turns)
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], text="Here is the list I meant.")

    await _deliver(agent, answer, _answer_meta(answer))

    # Not a decision: the question is asked again and no turn runs.
    [sent] = agent.team_comms.send_message.await_args_list
    asked_again = sent.kwargs["message"].msg_content
    assert (asked_again.query_type, asked_again.query_reason) == (
        MessageMetaType.REQUEST,
        TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
    )
    assert turns.prompts == []
    # The answer's text is kept on the task, and so are its files.
    stored = [query for query in agent.task_manager.get_task_queries(TASK) if query.query_type == MessageMetaType.RESPONSE]
    assert [(query.message, query.attachments) for query in stored] == [("Here is the list I meant.", [notes])]
    assert notes.attachment_id in _own(agent.task_manager, agent._conversation_store)


# --- Questions a plan step or a recovery task asks -------------------------------------------------


def _step_of_a_plan(agent: Any) -> Any:
    """The first step task of a two-step plan for TASK, as the lead runs it (its own conversation id, no turn)."""

    manager = agent.task_manager
    assert manager.activate_plan_execution(
        TASK,
        steps=[{"title": "Ask", "objective": "Ask which list."}, {"title": "Cook", "objective": "Cook."}],
        final_review=False,
    )
    [step] = [info for info in manager.task_state.tasks.values() if manager.is_plan_step_task(info.task.task_id)]
    return step


def _recovery_of(agent: Any) -> Any:
    """An isolated recovery task for TASK's plan, as the lead starts one (its own conversation id, no turn)."""

    _step_of_a_plan(agent)
    root = agent.task_manager.get_task_info(TASK)
    meta = root.meta.model_copy(update={"conversation_id": "recov_conv_1", "msg_correlation_id": "recovery_corr_1"})
    return agent.task_manager.start_isolated_recovery_task(
        TASK,
        failure_type="tool_failure",
        failure_summary="The shop's page did not load.",
        recovery_brief="Find another way to get the list.",
        header=root.header.model_copy(deep=True),
        meta=meta,
        notify_user=False,
    )


async def _answer_the_question_of(agent: Any, asking: Any, files: list[UserTaskAttachment]) -> None:
    """``asking``'s turn asks the person, who answers with ``files``; delivered as the gateway sends it.

    The runtime names the asking task as the question's parent, and the
    gateway signs the answer for that task and the conversation id of the
    question's message (the task's own).
    """

    task_id = asking.task.task_id
    question = _asked().model_copy(update={"parent_task_id": task_id})
    agent.task_manager.add_task_query(task_id, question, agent_name="Lead", agent_id=LEAD_ID)
    answer = _answer_to(files, task_id=task_id)
    signed = context_signer().issue(
        team_id=TEAM,
        person_id=ANN,
        person_roles=("member",),
        root_task_id=task_id,
        conversation_id=asking.meta.conversation_id,
        context_id=answer_context_id(answer),
    )
    meta = MessageMeta(
        conversation_id=asking.meta.conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=ANN, task_context=signed),
    )
    await _deliver(agent, answer, meta)


@pytest.mark.asyncio
@pytest.mark.parametrize("asking_task", [_step_of_a_plan, _recovery_of], ids=["plan_step", "recovery_task"])
async def test_an_answer_to_a_steps_question_keeps_its_files_on_the_request_too(
    asking_task: Any, tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner in steps.", attachments=[])
    asking = asking_task(agent)
    assert asking.meta.conversation_id != CONVERSATION
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    notes = _upload(layout, "list.png")

    with caplog.at_level(logging.INFO):
        await _answer_the_question_of(agent, asking, [notes])

    step_id = asking.task.task_id
    manager, store = agent.task_manager, agent._conversation_store
    # The step keeps the answer with its file, so its own turn opens it.
    assert manager.get_task_queries(step_id)[-1].attachments == [notes]
    assert _own(manager, store, step_id) == [notes.attachment_id]
    # The request has it too: on its conversation turn and among its own files.
    assert _turn_files(agent) == [notes.attachment_id]
    [entry] = _files_entries(manager)
    assert (entry.details["message_id"], entry.details["message_type"]) == ("task_query_list", QUESTION_ANSWER_MESSAGE_TYPE)
    assert step_id in entry.summary
    assert _own(manager, store) == [notes.attachment_id]
    assert not _messages(caplog, "conversation_turn_missing_for_attachments")

    # A follow-up request in the same chat, in a restarted lead, lists it and opens it by id.
    await _answer(agent, TASK, "Dinner is planned.")
    follow_up_agent = _lead(tmp_path, layout)
    await _admit(follow_up_agent, task_id="task_follow_up", text="What was on that list?", attachments=[])
    info = follow_up_agent.task_manager.get_task_info("task_follow_up")
    use_agent(follow_up_agent, "task_follow_up")
    listed = _sections(_prompt(follow_up_agent, info.task, info.header, info.meta))["conversation_attachments"]
    assert f"`{notes.attachment_id}` list.png" in listed
    opened = inspect_user_attachment._tool_func(attachment_id=notes.attachment_id)
    assert opened["status"] == "success", opened


def _point_root_at(manager: Any, step: Any, root_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = manager.resolve_root_task_id
    monkeypatch.setattr(
        manager, "resolve_root_task_id", lambda task_id: root_id if task_id == step.task.task_id else resolve(task_id)
    )


def _root_working_for_bob(manager: Any, step: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Bob's request, named as the step's root by a patched lookup: a step always works for its request's person."""

    assert manager.add_task(
        Task(task_id="task_bob", task_type=MessageMetaType.REQUEST, problem_statement="Bob's request."),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(conversation_id="conv_bob"),
        owner_person_id="usr_bob",
    )
    _point_root_at(manager, step, "task_bob", monkeypatch)
    return "task_bob"


def _root_delegated(manager: Any, step: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """A task delegated to this agent, named as the step's root by a patched lookup: plan links end at a request."""

    assert manager.add_task(
        Task(task_id="task_child", parent_task_id="task_parent", task_type=MessageMetaType.REQUEST, problem_statement="Parse it."),
        MessageHeader(from_agent_id="other_lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )
    _point_root_at(manager, step, "task_child", monkeypatch)
    return "task_child"


def _root_no_longer_held(manager: Any, step: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """The request itself, gone from the task manager while the step and its plan link remain."""

    del manager.task_state.tasks[TASK]
    assert manager.resolve_root_task_id(step.task.task_id) == TASK
    return TASK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_of", "reason"),
    [(_root_working_for_bob, "other_person"), (_root_delegated, "delegated"), (_root_no_longer_held, "not_held")],
    ids=["other_person", "delegated", "not_held"],
)
async def test_files_sent_to_a_step_stay_with_it_when_its_request_cannot_keep_them(
    root_of: Any,
    reason: str,
    tmp_path: Path,
    layout: InstanceLayout,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner in steps.", attachments=[])
    step = _step_of_a_plan(agent)
    manager = agent.task_manager
    root_id = root_of(manager, step, monkeypatch)
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    notes = _upload(layout, "list.png")

    with caplog.at_level(logging.WARNING):
        await _answer_the_question_of(agent, step, [notes])

    # The step takes the answer with its file; no request gets it.
    assert manager.get_task_queries(step.task.task_id)[-1].attachments == [notes]
    assert _own(manager, agent._conversation_store, step.task.task_id) == [notes.attachment_id]
    held = [task_id for task_id in dict.fromkeys((root_id, TASK)) if manager.get_task_info(task_id) is not None]
    assert all(_files_entries(manager, task_id) == [] for task_id in held)
    assert _turn_files(agent) == []
    [warning] = _messages(caplog, "sent_files_root_not_usable")
    assert f"task_id={step.task.task_id} root_task_id={root_id} reason={reason}" in warning


def _entries_end_for(manager: Any, ended_task_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ended_task_id`` ends while files are recorded: its history entries are not written (None)."""

    append = manager.append_history_entry
    monkeypatch.setattr(
        manager,
        "append_history_entry",
        lambda task_id, *args, **kwargs: None if task_id == ended_task_id else append(task_id, *args, **kwargs),
    )


@pytest.mark.asyncio
async def test_an_answer_to_a_request_that_ends_while_its_files_are_recorded_leaves_them_off_its_turn(
    asking_lead, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes])
    _entries_end_for(asking_lead.task_manager, TASK, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await _deliver(asking_lead, answer, _answer_meta(answer))

    # The request does not take the answer, so its conversation turn does not get the file.
    assert _messages(caplog, "task_query_task_gone")
    assert _turn_files(asking_lead) == []
    assert _own(asking_lead.task_manager, asking_lead._conversation_store) == []


@pytest.mark.asyncio
async def test_an_answer_to_a_step_that_ends_while_its_files_are_recorded_leaves_them_nowhere(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner in steps.", attachments=[])
    step = _step_of_a_plan(agent)
    manager, store = agent.task_manager, agent._conversation_store
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    notes = _upload(layout, "list.png")
    _entries_end_for(manager, step.task.task_id, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await _answer_the_question_of(agent, step, [notes])

    # The step does not take the answer, so its request keeps none of its files either.
    assert _messages(caplog, "task_query_task_gone")
    answers = [query for query in manager.get_task_queries(step.task.task_id) if query.query_type == MessageMetaType.RESPONSE]
    assert answers == []
    assert _own(manager, store, step.task.task_id) == []
    assert _files_entries(manager) == []
    assert _own(manager, store) == []
    assert _turn_files(agent) == []


@pytest.mark.asyncio
async def test_files_sent_to_a_step_whose_request_ends_meanwhile_stay_on_the_requests_turn(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Plan dinner in steps.", attachments=[])
    step = _step_of_a_plan(agent)
    step_id = step.task.task_id
    manager, store = agent.task_manager, agent._conversation_store
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    notes = _upload(layout, "list.png")
    _entries_end_for(manager, TASK, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await _answer_the_question_of(agent, step, [notes])

    # The step takes the answer with its file, and follow-up requests find it on the
    # request's conversation turn; the request's own files do not have it.
    assert manager.get_task_queries(step_id)[-1].attachments == [notes]
    assert _own(manager, store, step_id) == [notes.attachment_id]
    assert _turn_files(agent) == [notes.attachment_id]
    assert _files_entries(manager) == []
    [warning] = _messages(caplog, "sent_files_root_gone")
    assert "on its conversation turn and among that task's files, but not among the request's own files" in warning
    assert f"task_id={step_id} root_task_id={TASK} attachment_ids={notes.attachment_id}" in warning
