"""A Telegram reply carries the message it answers to the agent.

Reported 2026-10-12: a daily scheduled report reached the person's Telegram
group, they replied "@bot expected", and the agent answered "Your message came
through as just 'expected'". The report lives in its job's own conversation (or
in none, for a background check the manager runs itself), so the reply's
conversation never saw it, and the connector dropped Telegram's
``reply_to_message``. The connector now passes the replied message beside the
text (custom_messages.contracts.replied_message_v1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Update

import assistant_gateway.interactive_apps.telegram as telegram_module
from assistant_gateway.bootstrap import bootstrap_instance
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import (
    REPLIED_MESSAGE_MAX_CHARS,
    RepliedMessage,
)
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskStatus
from custom_messages.enums import MessageMetaType, RequesterType
from tests.application_test_doubles import application_from as GatewayApplicationFacade
from tests.test_gateway_telegram_app import (
    TelegramInteractiveApp,
    _FakeApp,
    _FakeBridge,
    _FakeGateway,
    _FakeVoiceClient,
    _config,
    _make_update,
)

BOT_ID = 999
PERSON_ID = 777
REPORT = (
    "Daily Gmail check (last 24 hours): 7 new emails, nothing urgent.\n"
    "- 1 Google security alert to review.\n"
    'Reply "expected" if you made this change, and I\'ll note it.'
)
SENT_AT = datetime(2026, 10, 12, 13, 3, tzinfo=timezone.utc)


def _rich_update(*, blocks=None) -> Update:
    """Decode the actual Bot API form, including PTB's untyped rich-message field."""
    chat = {"id": -100123, "type": "supergroup", "title": "Team"}
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 50,
                "date": int(SENT_AT.timestamp()),
                "chat": chat,
                "from": {"id": PERSON_ID, "is_bot": False, "first_name": "Ana"},
                "text": "expected",
                "reply_to_message": {
                    "message_id": 41,
                    "date": int(SENT_AT.timestamp()),
                    "chat": chat,
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Team"},
                    "rich_message": {"blocks": blocks or [{"type": "paragraph", "text": REPORT}]},
                },
            },
        },
        bot=None,
    )


def _replied(*, sender_id: int | None = BOT_ID, text: str | None = REPORT, **fields):
    values = {
        "message_id": 41,
        "text": text,
        "caption": None,
        "from_user": SimpleNamespace(id=sender_id) if sender_id is not None else None,
        "sender_chat": None,
        "date": SENT_AT,
        "forum_topic_created": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _app(tmp_path: Path, bridge, *, application=None) -> TelegramInteractiveApp:
    config = _config(tmp_path)
    if application is None:
        application = GatewayApplicationFacade(
            _FakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
        )
    app = TelegramInteractiveApp(config, application=application, bridge=bridge)
    app._app = _FakeApp()
    app._bot_user_id = BOT_ID
    app._bot_username = "team_test_bot"
    return app


async def _reply(
    app: TelegramInteractiveApp, replied, *, text: str = "@team_test_bot  expected", quote=None, message_id: int = 50
):
    update = _make_update(text=text, message_id=message_id, user_id=PERSON_ID)
    update.effective_message.reply_to_message = replied
    update.effective_message.quote = quote
    await app._handle_update_text(update, text=text)


@pytest.mark.asyncio
async def test_a_reply_to_the_bots_report_reaches_the_gateway_with_the_report(tmp_path, caplog) -> None:
    bridge = _FakeBridge()
    app = _app(tmp_path, bridge)

    with caplog.at_level(logging.INFO, logger=telegram_module.__name__):
        await _reply(app, _replied())

    [inbound] = bridge.inbound_messages
    assert inbound.text == "expected"
    assert inbound.reply_to_message_id == "41"
    assert inbound.replied_message == RepliedMessage(
        author="assistant", text=REPORT, sent_at="2026-10-12T13:03:00+00:00"
    )
    # The log names the reply without copying the report's text.
    assert "author=assistant" in caplog.text and "Google security alert" not in caplog.text


@pytest.mark.asyncio
async def test_rich_reply_keeps_nested_text_lists_tables_and_media_captions(tmp_path) -> None:
    bridge = _FakeBridge()
    app = _app(tmp_path, bridge)
    update = _rich_update(blocks=[
        {"type": "heading", "size": 2, "text": ["Daily ", {"type": "bold", "text": "Gmail check"}]},
        {"type": "list", "items": [
            {"label": "1.", "has_checkbox": True, "is_checked": True, "blocks": [
                {"type": "paragraph", "text": ["Review ", {"type": "url", "text": "Google access", "url": "https://hidden.example"}]},
            ]},
        ]},
        {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "Current Request:"}], "credit": "Email"},
        {"type": "table", "cells": [[{"text": "Status"}, {"text": {"type": "italic", "text": "Expected"}}]], "caption": "Account changes"},
        {"type": "photo", "photo": [], "caption": {"text": "Alert screenshot", "credit": "Dana"}},
        {"type": "details", "summary": "More detail", "blocks": [{"type": "paragraph", "text": "One alert"}]},
        {"type": "paragraph", "text": [{"type": "custom_emoji", "custom_emoji_id": "private_id", "alternative_text": "OK"}, " ", {"type": "mathematical_expression", "expression": "x=1"}]},
        {"type": "divider"},
    ])
    await app._handle_update_text(update, text=update.effective_message.text)

    [inbound] = bridge.inbound_messages
    assert inbound.replied_message.text == (
        "Daily Gmail check\n1. [x] Review Google access\nCurrent Request:\nEmail\n"
        "Status | Expected\nAccount changes\nAlert screenshot\nDana\nMore detail\nOne alert\nOK x=1"
    )
    assert inbound.replied_message.author == "assistant"


@pytest.mark.asyncio
async def test_long_rich_reply_is_bounded_and_unknown_rich_content_is_logged(tmp_path, caplog) -> None:
    bridge = _FakeBridge()
    app = _app(tmp_path, bridge)
    update = _rich_update(blocks=[{"type": "paragraph", "text": "x" * (REPLIED_MESSAGE_MAX_CHARS + 1)}])
    await app._handle_update_text(update, text=update.effective_message.text)
    assert bridge.inbound_messages[0].replied_message.shortened is True
    assert len(bridge.inbound_messages[0].replied_message.text) == REPLIED_MESSAGE_MAX_CHARS

    from assistant_gateway.interactive_apps.telegram_reply_text import replied_message_text

    with caplog.at_level(logging.WARNING):
        text = replied_message_text(_rich_update(blocks=[{"type": "future_block", "private_metadata": "hidden"}]).effective_message.reply_to_message)
    assert text == ""
    assert "event=telegram_reply_rich_block_unsupported" in caplog.text
    assert "hidden" not in caplog.text


@pytest.mark.parametrize(
    ("replied", "author", "author_name"),
    [
        pytest.param(_replied(sender_id=PERSON_ID), "requester", None, id="own-message"),
        pytest.param(
            _replied(sender_id=555, from_user=SimpleNamespace(id=555, first_name="Dana", last_name="Reyes")),
            "other",
            "Dana Reyes",
            id="another-member",
        ),
        pytest.param(
            _replied(sender_id=136817688, sender_chat=SimpleNamespace(title="Launch News")),
            "other",
            "Launch News",
            id="posted-as-a-channel",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_replied_message_names_who_wrote_it(tmp_path, replied, author, author_name) -> None:
    bridge = _FakeBridge()
    await _reply(_app(tmp_path, bridge), replied)

    replied_message = bridge.inbound_messages[0].replied_message
    assert (replied_message.author, replied_message.author_name) == (author, author_name)


@pytest.mark.asyncio
async def test_quotes_captions_and_long_reports(tmp_path, caplog) -> None:
    bridge = _FakeBridge()
    app = _app(tmp_path, bridge)

    await _reply(app, _replied(), quote=SimpleNamespace(text="1 Google security alert"))
    await _reply(app, _replied(text=None, caption="Launch photo"), text="@team_test_bot nice", message_id=51)
    with caplog.at_level(logging.INFO, logger=telegram_module.__name__):
        await _reply(
            app, _replied(text="x" * (REPLIED_MESSAGE_MAX_CHARS + 5)), text="@team_test_bot ok", message_id=52
        )

    quoted, captioned, long = (message.replied_message for message in bridge.inbound_messages)
    assert quoted.quoted_text == "1 Google security alert"
    assert captioned.text == "Launch photo"
    assert long.shortened is True and len(long.text) == REPLIED_MESSAGE_MAX_CHARS
    assert "shortened=True" in caplog.text


@pytest.mark.parametrize(
    ("replied", "reply_to_message_id"),
    [
        # Telegram fills reply_to_message with a forum topic's opening service
        # message for every message in that topic; that is not a reply.
        pytest.param(_replied(text=None, forum_topic_created=SimpleNamespace(name="Gmail")), None, id="forum-topic"),
        # A sticker or a photo without caption: the id stays, nothing to read.
        pytest.param(_replied(text=None), "41", id="no-text"),
    ],
)
@pytest.mark.asyncio
async def test_replies_without_text_to_pass_on(tmp_path, replied, reply_to_message_id) -> None:
    bridge = _FakeBridge()
    await _reply(_app(tmp_path, bridge), replied)

    [inbound] = bridge.inbound_messages
    assert (inbound.reply_to_message_id, inbound.replied_message) == (reply_to_message_id, None)


@pytest.mark.asyncio
async def test_an_unknown_bot_id_passes_nothing_rather_than_mislabeling_the_report(tmp_path, caplog) -> None:
    bridge = _FakeBridge()
    app = _app(tmp_path, bridge)
    app._bot_user_id = None
    app.config.group_policy = "open"

    with caplog.at_level(logging.WARNING, logger=telegram_module.__name__):
        await _reply(app, _replied())

    assert bridge.inbound_messages[0].replied_message is None
    assert "event=telegram_reply_context_bot_id_missing" in caplog.text


@pytest.mark.asyncio
async def test_a_voice_reply_carries_the_replied_message(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(telegram_module, "build_voice_client", lambda _settings: _FakeVoiceClient(), raising=False)
    config = _config(tmp_path)
    config.voice.enabled = True
    config.voice.input_enabled = True
    config.interactive_apps.telegram.group_policy = "open"
    bridge = _FakeBridge()
    app = TelegramInteractiveApp(
        config,
        application=GatewayApplicationFacade(_FakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))),
        bridge=bridge,
    )
    app._app = _FakeApp()
    app._app.bot.files["voice_file"] = b"ogg-voice"
    app._bot_user_id = BOT_ID
    update = _make_update(text="", voice=SimpleNamespace(file_id="voice_file", mime_type="audio/ogg"))
    update.effective_message.reply_to_message = _replied()

    await app._on_message(update, SimpleNamespace())

    [inbound] = bridge.inbound_messages
    assert inbound.text == "transcribed voice request"
    assert inbound.replied_message.text == REPORT


@pytest.mark.asyncio
async def test_an_attachment_only_reply_keeps_the_replied_message() -> None:
    sent: list[dict] = []

    class _Application:
        async def has_pending_query(self, _session_key):
            return False

        @staticmethod
        def is_task_progress_status_request(_text, _attachments=None):
            return False

        async def send_user_message(self, text, **kwargs):
            sent.append({"text": text, **kwargs})
            return SimpleNamespace(text="ok", conversation_id="conv", correlation_id="corr", message_type="task")

    attachment = UserTaskAttachment(
        attachment_id="att_1", filename="photo.jpg", content_type="image/jpeg", kind="image",
        size_bytes=3, sha256="a" * 64, origin="telegram",
    )
    replied = RepliedMessage(author="assistant", text=REPORT)
    await ChannelBridge(_Application()).handle_inbound(
        ChannelInboundMessage(
            channel_name="telegram", user_id="777", chat_id="-100123", text="", session_key="s",
            message_id="50", attachments=[attachment], reply_to_message_id="41", replied_message=replied,
        )
    )

    assert sent[0]["replied_message"] == replied


class _ManagerTransport:
    """Answers each published request like the manager, keeping what it received."""

    def __init__(self) -> None:
        self.gateway: UserGateway | None = None
        self.durable: list[MessageEnvelope] = []

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        if isinstance(envelope.msg_content, TaskStatus):
            assert self.gateway is not None
            await self.gateway._manager_client.handle_response(
                envelope.model_copy(update={
                    "msg_content": envelope.msg_content.model_copy(update={
                        "status_type": MessageMetaType.RESPONSE, "is_known_task": True,
                    }),
                    "msg_header": MessageHeader(from_agent_id="manager_agent_1"),
                })
            )

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        self.durable.append(envelope)
        task = envelope.msg_content
        assert self.gateway is not None and isinstance(task, (Task, TaskQuery))
        task_id = task.task_id if isinstance(task, Task) else task.parent_task_id
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=Task(task_id=task_id, task_type=MessageMetaType.RESPONSE, answer="Noted."),
                msg_header=MessageHeader(from_agent_id="manager_agent_1", to_routing_path="reply_gateway", agent_signature=""),
                msg_meta=envelope.msg_meta.model_copy(update={"requester_type": RequesterType.USER_REQUESTER}),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True], ids=["text", "rich"])
@pytest.mark.parametrize("clarification", [False, True], ids=["request", "clarification"])
async def test_the_replied_report_reaches_the_manager_on_the_request(tmp_path: Path, rich, clarification) -> None:
    """Telegram connector, channel bridge and the real gateway application, with a fake manager."""

    config = _config(tmp_path)
    # Links the Telegram account to the instance's person, as onboarding does.
    config.interactive_apps.telegram.allowed_user_ids = [str(PERSON_ID)]
    config.manager.manager_agent_id = "manager_agent_1"
    config.manager.response_queue_name = "reply_gateway"
    config.save()
    bootstrap_instance(config)
    transport = _ManagerTransport()
    gateway = UserGateway(config, state_store=GatewayStateStore(str(tmp_path / "gateway.db")), manager_transport=transport)
    transport.gateway = gateway
    application = GatewayApplicationFacade(gateway, command_application=gateway.command_application)
    app = _app(tmp_path, ChannelBridge(application), application=application)

    session = application.resolve_session(channel_name="telegram", user_id=str(PERSON_ID), chat_id="-100123")
    if clarification:
        query = TaskQuery(query_type=MessageMetaType.REQUEST, parent_task_id="task_paused", message="Which report?")
        gateway.state_store.upsert_pending_query(
            session_key=session.session_key, correlation_id="corr_question", task_query_id=query.task_query_id,
            parent_task_id=query.parent_task_id, conversation_id=session.conversation_id, query_text=query.message,
            query_payload_json=query.model_dump_json(),
        )

    if rich:
        update = _rich_update()
        await app._handle_update_text(update, text=update.effective_message.text)
    else:
        await _reply(app, _replied())

    [published] = transport.durable
    task = published.msg_content
    if clarification:
        assert isinstance(task, TaskQuery)
        assert task.message == "expected" and task.parent_task_id == "task_paused"
    else:
        assert isinstance(task, Task) and task.problem_statement == "expected", "the person's words stay their own"
    assert task.replied_message == RepliedMessage(author="assistant", text=REPORT, sent_at="2026-10-12T13:03:00+00:00")
    assert published.msg_meta.requester_type == RequesterType.USER_REQUESTER
    assert published.msg_meta.conversation_id == session.conversation_id
    assert app._app.bot.sent_messages[-1]["text"] == "Noted."
