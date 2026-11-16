"""Files a person sends with an answer in Web Chat, through the real gateway.

The real ``UserGateway`` with its SQLite store and Web Chat; only the lead is a
transport that records what the gateway sends (``tests.test_gateway_question_queue``).
Web Chat saves an answer's files after its owner checks, shows them with the
answer in the transcript, sends them with the answer (``TaskQuery.attachments``)
and saves nothing new on a browser retry; a message sent before the question
was shown says its files were not sent. Ported from #137; the chat bridge's
cases are main's tests in ``tests.test_question_answer_files``.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.clarification import ANSWER_FILES_NOT_SENT_TEXT
from assistant_gateway.application.pending_questions import QUESTION_WAITING_INTRO_TEXT
from assistant_gateway.channel_bridge import ATTACHMENT_ONLY_TASK_TEXT
from tests.test_gateway_question_queue import REPORT_QUESTION, TRIP_QUESTION, _Chat, _two_requests_ask
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp


def _ask_unseen(chat: _Chat) -> str:
    """A question waiting in the chat that the person has not been shown yet."""

    chat.record_request("corr_report", "Summarize the sales report")
    chat.store.add_pending_query(
        session_key=chat.session.session_key, correlation_id="corr_report", task_query_id="question_unseen",
        parent_task_id="task_report", conversation_id=chat.session.conversation_id, query_text=REPORT_QUESTION,
    )
    return "question_unseen"


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
        assert (answers[1].message, week.filename, week.kind) == (ATTACHMENT_ONLY_TASK_TEXT, "week.png", "image")
        # A browser retry of the answer sends and saves nothing new.
        assert retried["messages"][-1]["message_id"] == answered["messages"][-1]["message_id"]
        assert len(list(chat.config.layout.uploads_dir.rglob("*.pdf"))) == 1
        # The transcript shows each answer with its file.
        sent = {message["content"]: message for message in transcript if message["role"] == "user"}
        assert [item["filename"] for item in sent["This is the report"]["attachments"]] == ["report.pdf"]
        # A file-only answer shows the person's empty text with its file.
        assert [item["filename"] for item in sent[""]["attachments"]] == ["week.png"]
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
        assert reply["messages"][-1]["content"] == (
            f"{ANSWER_FILES_NOT_SENT_TEXT}\n\n{QUESTION_WAITING_INTRO_TEXT}\n\n{REPORT_QUESTION}"
        )
        # The transcript shows what the person sent, then the note, which leads with the files.
        [sent] = [message for message in transcript if message["role"] == "user"]
        assert (sent["content"], [item["filename"] for item in sent["attachments"]]) == ("Here it is", ["report.pdf"])
        assert transcript[-1]["content"].startswith(ANSWER_FILES_NOT_SENT_TEXT)
        assert chat.open_question_ids() == [question_id]

    await _web_chat(chat, monkeypatch, test)

