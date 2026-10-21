from __future__ import annotations

import pytest

from assistant_gateway.application import (
    GatewayApplication,
    GatewayApplicationResponse,
    GatewayCommandResult,
    GatewayScheduledJob,
    GatewaySession,
)
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.translation import (
    ChannelInboundMessage,
    DeterministicTextTranslator,
)


class FakeGatewayApplication:
    default_session_key = "default"

    def __init__(self) -> None:
        self.sent_user_messages: list[tuple[str, str]] = []
        self.query_replies: list[tuple[str, str]] = []
        self.replied_messages: list = []
        self.added_jobs: list[dict] = []
        self.deliveries_released: list[str] = []
        self.scheduled_jobs: list[GatewayScheduledJob] = []
        self.started_sessions: list[str] = []
        self.status_requests: list[str] = []
        # Session key to the id of the question waiting there.
        self.waiting_questions: dict[str, str] = {}
        self.sessions: dict[str, GatewaySession] = {
            "session_1": GatewaySession(
                session_key="session_1",
                conversation_id="conv_1",
            )
        }
        self.active_session_keys: dict[tuple[str, str, str, str | None], str] = {}

    def get_active_session(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> GatewaySession | None:
        session_key = self.active_session_keys.get(
            (channel_name, user_id, chat_id, thread_id)
        )
        return self.sessions.get(session_key) if session_key else None

    @staticmethod
    def is_registered_user(
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool:
        return bool(channel_name and external_user_id and chat_scope == "")

    async def execute_interactive_command(self, *, raw_text: str, **_kwargs):
        return GatewayCommandResult(
            handled=raw_text.startswith("/"),
            session_key="session_1",
            text=None,
        )

    @staticmethod
    def is_task_progress_status_request(text: str, attachments=None) -> bool:
        return str(text or "").strip() == "?" and not attachments

    async def send_user_message(
        self,
        text: str,
        *,
        session_key=None,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        original_message_id=None,
        attachments=None,
        replied_message=None,
    ):
        resolved_session_key = session_key or "session_1"
        self.replied_messages.append(replied_message)
        self.sent_user_messages.append((resolved_session_key, text))
        return GatewayApplicationResponse(
            text=f"task:{text}",
            conversation_id="conv_1",
            correlation_id="corr_response",
            message_type="task",
        )

    async def decide_access_review(self, _request):
        return None

    async def reply_to_pending_query(
        self, *, session_key: str, task_query_id: str, response_text: str, replied_message=None
    ):
        assert self.waiting_questions.pop(session_key) == task_query_id
        self.query_replies.append((session_key, response_text))
        return GatewayApplicationResponse(
            text=f"query_reply:{response_text}",
            conversation_id="conv_1",
        )

    async def request_task_progress_status(self, *, session_key=None, **_kwargs):
        self.status_requests.append(session_key or "session_1")
        return GatewayApplicationResponse(
            text="Task status: waiting.",
            conversation_id="conv_1",
        )

    async def waiting_question_id(self, session_key: str) -> str | None:
        return self.waiting_questions.get(session_key)

    async def add_cron_job(
        self,
        *,
        session_key=None,
        name: str,
        instruction: str,
        interval_seconds=None,
        cron_expression=None,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        original_message_id=None,
    ):
        resolved_session_key = session_key or "session_1"
        self.added_jobs.append(
            {
                "session_key": resolved_session_key,
                "name": name,
                "instruction": instruction,
                "interval_seconds": interval_seconds,
                "cron_expression": cron_expression,
            }
        )
        self.scheduled_jobs.append(
            GatewayScheduledJob(
                scheduled_job_id="cron_test_1234",
                next_fire_at=None,
                task_summary=instruction,
            )
        )
        return "cron_test_1234"

    async def release_due_deliveries(self):
        return list(self.deliveries_released)

    async def list_scheduled_jobs(self, **_kwargs):
        return list(self.scheduled_jobs)

    async def pause_scheduled_job(self, _schedule_id, **_kwargs):
        return True

    async def resume_scheduled_job(self, _schedule_id, **_kwargs):
        return True

    async def disable_scheduled_job(self, _schedule_id, **_kwargs):
        return True

    async def remove_scheduled_job(self, _schedule_id, **_kwargs):
        return True

    @staticmethod
    def requester_timezone_for_context(**_kwargs) -> str:
        return "America/Chicago"

    def start_new_session(
        self, *, channel_name=None, user_id=None, chat_id=None, thread_id=None
    ):
        session = GatewaySession(
            session_key="session_new",
            conversation_id="conversation_id_new",
        )
        self.sessions[session.session_key] = session
        if channel_name and user_id and chat_id:
            self.active_session_keys[(channel_name, user_id, chat_id, thread_id)] = (
                session.session_key
            )
        self.started_sessions.append("session_new")
        return session

    def resolve_session(
        self,
        *,
        session_key=None,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
    ):
        resolved_session_key = session_key or "session_1"
        existing = self.sessions.get(resolved_session_key)
        if existing is not None:
            return existing
        session = GatewaySession(
            session_key=resolved_session_key,
            conversation_id="conv_1",
        )
        self.sessions[resolved_session_key] = session
        return session

    @staticmethod
    def register_inbound_attachment(
        attachment,
        *,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        return None


def _inbound(text: str, session_key: str | None = "session_1") -> ChannelInboundMessage:
    return ChannelInboundMessage(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        text=text,
        session_key=session_key,
        message_id="msg_1",
    )


def test_translator_maps_interval_reminder_to_cron():
    translator = DeterministicTextTranslator()
    action = translator.translate_inbound(
        _inbound("remind me every 2 minutes to stretch")
    )
    assert action.action_type == "add_cron_job"
    assert action.interval_seconds == 120
    assert action.cron_instruction == "stretch"


def test_fake_gateway_application_implements_bridge_contract():
    assert isinstance(FakeGatewayApplication(), GatewayApplication)


@pytest.mark.asyncio
async def test_bridge_renders_task_response():
    application = FakeGatewayApplication()
    bridge = ChannelBridge(application)

    messages = await bridge.handle_inbound(_inbound("Explain recursion"))

    assert application.sent_user_messages == [("session_1", "Explain recursion")]
    assert messages[0].text == "task:Explain recursion"
    assert messages[0].correlation_id == "corr_response"
    assert messages[0].message_type == "task"


@pytest.mark.asyncio
async def test_bridge_answers_the_waiting_question_by_its_id():
    application = FakeGatewayApplication()
    application.waiting_questions["session_1"] = "task_query_1"
    bridge = ChannelBridge(application)

    messages = await bridge.handle_inbound(_inbound("show my scheduled jobs"))

    # While a question waits, even a command-like phrase is its answer.
    assert application.query_replies == [("session_1", "show my scheduled jobs")]
    assert application.sent_user_messages == []
    assert messages[0].text == "query_reply:show my scheduled jobs"


@pytest.mark.asyncio
async def test_bridge_lists_cron_jobs_and_deliveries():
    application = FakeGatewayApplication()
    bridge = ChannelBridge(application)

    application.scheduled_jobs.append(
        GatewayScheduledJob(
            scheduled_job_id="cron_test_1234",
            next_fire_at="2026-08-03T21:21:01+00:00",
            task_summary="do thing",
        )
    )
    list_messages = await bridge.handle_inbound(_inbound("show my scheduled jobs"))
    assert "Scheduled jobs:" in list_messages[0].text
    assert "Aug 3, 2026 at 4:21:01 PM CDT" in list_messages[0].text
    assert "2026-08-03T21:21:01+00:00" not in list_messages[0].text

    application.deliveries_released = ["Deferred summary (1 items):\n1. hello"]
    delivery_messages = await bridge.handle_inbound(
        _inbound("deliver pending messages")
    )
    assert "Deferred summary" in delivery_messages[0].text
