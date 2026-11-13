"""Files a person sends with an answer to an agent's question, through the real gateway.

The real ``UserGateway`` with its SQLite store, the chat bridge and Web Chat;
only the lead is a transport that records what the gateway sends
(``tests.test_gateway_question_queue``). An answer carries the files the
gateway stored for that chat (``TaskQuery.attachments``), a file-only answer
gets the usual file-only text, and a message that does not answer the
question (not shown yet, already answered) says its files were not sent.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.clarification import ANSWER_FILES_NOT_SENT_TEXT
from assistant_gateway.application.pending_questions import (
    ALREADY_ANSWERED_TEXT,
    QUESTION_WAITING_INTRO_TEXT,
)
from assistant_gateway.channel_bridge import ATTACHMENT_ONLY_TASK_TEXT, ChannelBridge
from assistant_gateway.translation import ChannelInboundMessage
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from tests.test_gateway_question_queue import (
    REPORT_QUESTION,
    TELEGRAM_ID,
    TRIP_QUESTION,
    _Chat,
    _two_requests_ask,
)
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp

FILES_NOT_SENT = f"\n\n{ANSWER_FILES_NOT_SENT_TEXT}"


def _stored_file(chat: _Chat, attachment_id: str, filename: str) -> UserTaskAttachment:
    """A file the chat adapter stored for this chat, as it hands it to the bridge."""

    path = chat.config.layout.uploads_dir / "telegram" / attachment_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"photo")
    return UserTaskAttachment(
        attachment_id=attachment_id, filename=filename, content_type="image/jpeg", kind="image",
        size_bytes=5, sha256="0" * 64, storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


async def _person_sends(chat: _Chat, text: str, files: list[UserTaskAttachment]):
    return await ChannelBridge(chat.gateway.application).handle_inbound(
        ChannelInboundMessage(
            channel_name="telegram", user_id=TELEGRAM_ID, chat_id=TELEGRAM_ID,
            session_key=chat.session.session_key, text=text, attachments=files,
        )
    )


def _ask_unseen(chat: _Chat) -> str:
    """A question waiting in the chat that the person has not been shown yet."""

    chat.record_request("corr_report", "Summarize the sales report")
    chat.store.add_pending_query(
        session_key=chat.session.session_key, correlation_id="corr_report", task_query_id="question_unseen",
        parent_task_id="task_report", conversation_id=chat.session.conversation_id, query_text=REPORT_QUESTION,
    )
    return "question_unseen"


@pytest.mark.asyncio
async def test_a_chat_app_answer_carries_its_files_and_a_file_only_answer_gets_the_file_only_text(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    receipt = _stored_file(chat, "att_receipt", "receipt.jpg")
    map_photo = _stored_file(chat, "att_map", "map.jpg")

    replies = await _person_sends(chat, "This is the report folder", [receipt])
    await _person_sends(chat, "", [map_photo])

    answers = chat.lead.answers()
    assert [answer.task_query_id for answer in answers] == [first.task_query_id, second.task_query_id]
    assert (answers[0].message, answers[0].attachments) == ("This is the report folder", [receipt])
    assert (answers[1].message, answers[1].attachments) == (ATTACHMENT_ONLY_TASK_TEXT, [map_photo])
    assert replies[0].text == "Done with task_report"
    assert chat.open_question_ids() == []


@pytest.mark.asyncio
async def test_a_chat_app_message_with_files_for_a_question_not_shown_yet_says_they_were_not_sent(
    tmp_path: Path,
) -> None:
    chat = _Chat(tmp_path)
    question_id = _ask_unseen(chat)

    replies = await _person_sends(chat, "Here it is", [_stored_file(chat, "att_receipt", "receipt.jpg")])

    assert chat.lead.answers() == []
    assert replies[0].text == f"{QUESTION_WAITING_INTRO_TEXT}\n\n{REPORT_QUESTION}{FILES_NOT_SENT}"
    assert chat.open_question_ids() == [question_id]


@pytest.mark.asyncio
async def test_a_second_answer_with_files_says_they_were_not_sent(tmp_path: Path) -> None:
    chat = _Chat(tmp_path)
    first, second = await _two_requests_ask(chat)
    chat.finish_each_answered_task()
    application = chat.gateway.application
    # Two messages found the same question before either answered it.
    question_id = await application.waiting_question_id(chat.session.session_key)
    receipt = _stored_file(chat, "att_receipt", "receipt.jpg")

    await application.reply_to_pending_query(
        session_key=chat.session.session_key, task_query_id=question_id, response_text="The folder",
        attachments=(receipt,),
    )
    duplicate = await application.reply_to_pending_query(
        session_key=chat.session.session_key, task_query_id=question_id, response_text="The folder",
        attachments=(_stored_file(chat, "att_again", "again.jpg"),),
    )
    # Without files, the refusal is unchanged.
    plain = await application.reply_to_pending_query(
        session_key=chat.session.session_key, task_query_id=question_id, response_text="The folder",
    )

    assert duplicate.text == f"{ALREADY_ANSWERED_TEXT}{FILES_NOT_SENT}"
    assert plain.text == ALREADY_ANSWERED_TEXT
    assert [(answer.task_query_id, answer.attachments) for answer in chat.lead.answers()] == [
        (first.task_query_id, [receipt])
    ]


class _WebChat:
    """Web Chat on the real gateway, signed in as the instance's person, in one new conversation."""

    def __init__(self, chat: _Chat, client: TestClient, headers: dict[str, str], session_key: str) -> None:
        self.chat = chat
        self.client = client
        self.headers = headers
        self.session_key = session_key

    async def send(self, text: str, request_id: str, files: tuple[tuple[str, bytes, str], ...] = ()) -> dict:
        form = FormData()
        form.add_field("message", text)
        form.add_field("session_key", self.session_key)
        form.add_field("client_request_id", request_id)
        for filename, data, content_type in files:
            form.add_field("attachments[]", BytesIO(data), filename=filename, content_type=content_type)
        response = await self.client.post("/api/web-chat/chat", headers=self.headers, data=form)
        assert response.status == 200, await response.text()
        return await response.json()

    async def transcript(self) -> list[dict]:
        response = await self.client.get(f"/api/web-chat/sessions/{self.session_key}/messages", headers=self.headers)
        return (await response.json())["messages"]


async def _web_chat(chat: _Chat, monkeypatch: pytest.MonkeyPatch, test):
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
        await test(_WebChat(chat, client, headers, session_key))


@pytest.mark.asyncio
async def test_web_chat_saves_an_answers_files_shows_them_and_sends_them_with_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _Chat(tmp_path, channel="web")

    async def test(web: _WebChat) -> None:
        first, second = await _two_requests_ask(chat)
        chat.finish_each_answered_task()

        answered = await web.send("This is the report", "answer_1", (("report.pdf", b"%PDF-1", "application/pdf"),))
        retried = await web.send("This is the report", "answer_1", (("report.pdf", b"%PDF-1", "application/pdf"),))
        await web.send("", "answer_2", (("week.png", b"\x89PNG", "image/png"),))
        transcript = await web.transcript()

        answers = chat.lead.answers()
        assert [answer.task_query_id for answer in answers] == [first.task_query_id, second.task_query_id]
        [report] = answers[0].attachments
        assert (answers[0].message, report.filename, report.kind) == ("This is the report", "report.pdf", "document")
        # Stored for this chat in the instance's uploads folder, as any sent file.
        assert chat.config.layout.uploads_dir in Path(report.local_path()).parents
        assert Path(report.local_path()).read_bytes() == b"%PDF-1"
        assert chat.store.get_attachment(report.attachment_id) is not None
        [week] = answers[1].attachments
        assert (answers[1].message, week.filename, week.kind) == ("User sent attachment(s).", "week.png", "image")
        # A browser retry of the answer sends and saves nothing new.
        assert retried["messages"][-1]["message_id"] == answered["messages"][-1]["message_id"]
        assert len(list(chat.config.layout.uploads_dir.rglob("*.pdf"))) == 1
        # The transcript shows each answer with its file.
        sent = {message["content"]: message for message in transcript if message["role"] == "user"}
        assert [item["filename"] for item in sent["This is the report"]["attachments"]] == ["report.pdf"]
        assert [item["filename"] for item in sent["User sent attachment(s)."]["attachments"]] == ["week.png"]
        contents = [message["content"] for message in transcript]
        assert contents.index("This is the report") < contents.index(TRIP_QUESTION)

    await _web_chat(chat, monkeypatch, test)


@pytest.mark.asyncio
async def test_web_chat_files_sent_before_the_question_was_shown_are_not_sent_and_the_reply_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _Chat(tmp_path, channel="web")

    async def test(web: _WebChat) -> None:
        question_id = _ask_unseen(chat)

        reply = await web.send("Here it is", "early_1", (("report.pdf", b"%PDF-1", "application/pdf"),))
        transcript = await web.transcript()

        assert chat.lead.answers() == []
        assert reply["messages"][-1]["content"] == f"{QUESTION_WAITING_INTRO_TEXT}\n\n{REPORT_QUESTION}{FILES_NOT_SENT}"
        # The transcript shows what the person sent, then the note.
        [sent] = [message for message in transcript if message["role"] == "user"]
        assert (sent["content"], [item["filename"] for item in sent["attachments"]]) == ("Here it is", ["report.pdf"])
        assert transcript[-1]["content"].endswith(ANSWER_FILES_NOT_SENT_TEXT)
        assert chat.open_question_ids() == [question_id]

    await _web_chat(chat, monkeypatch, test)

