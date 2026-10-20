from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_gateway.application import GatewayApplication, WebsiteGatewayApplication
from assistant_gateway.application.gateway_application import GatewayCommandResult
from assistant_gateway.application.runtime_application import (
    GatewayRuntimeApplication,
    GatewayRuntimeApplicationSettings,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


def _session_record() -> SessionRecord:
    return SessionRecord(
        session_key="session_1",
        session_alias=None,
        conversation_id="conversation_1",
        last_task_id=None,
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        thread_id=None,
        status="active",
        created_at="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:00+00:00",
    )


class _SessionRepository:
    @staticmethod
    def get_active_session_for_channel(**_kwargs) -> SessionRecord:
        return _session_record()


class _DeliveryRepository:
    def __init__(self) -> None:
        self.attachment_writes: list[dict[str, object]] = []

    def upsert_attachment(self, **values: object) -> None:
        self.attachment_writes.append(values)


class _RequestContext:
    users_enabled = True

    @staticmethod
    def resolve_canonical_user(route):
        if route.channel_name == "telegram" and route.user_id == "user_1":
            return SimpleNamespace()
        return None


class _Commands:
    async def execute(self, request) -> GatewayCommandResult:
        if (
            request.raw_text == "/reply msg_1 continue"
            and not request.enable_reply_command
        ):
            return GatewayCommandResult(
                handled=True,
                session_key=request.current_session_key,
                text="The /reply command is available only in the local CLI.",
            )
        return GatewayCommandResult(
            handled=False,
            session_key=request.current_session_key,
            text=None,
        )


class _Progress:
    @staticmethod
    def is_progress_request(text, attachments=None) -> bool:
        return str(text or "").strip() == "?" and not attachments


class _Clarification:
    @staticmethod
    async def waiting_question_id(session_key: str) -> str | None:
        return "task_query_1" if session_key == "session_with_query" else None


class _Schedules:
    @staticmethod
    async def list(**_kwargs) -> list[ScheduledMessage]:
        return [
            ScheduledMessage(
                schedule_id="schedule_1",
                next_fire_at="2026-07-27T00:00:00+00:00",
                payload=Task(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Context line\nSend the report",
                ),
                payload_meta=MessageMeta(
                    gateway_context=GatewayContext(
                        session_key="session_1",
                        scheduled_job_id="friendly_schedule_id",
                    )
                ),
            )
        ]


def _application_dependencies() -> tuple[dict[str, object], _DeliveryRepository]:
    delivery_repository = _DeliveryRepository()
    dependencies: dict[str, object] = {
        "settings": GatewayRuntimeApplicationSettings(
            default_session_key="default_session",
            default_owner_canonical_user_id="canonical_user_1",
        ),
        "sessions": SimpleNamespace(),
        "session_repository": _SessionRepository(),
        "delivery_repository": delivery_repository,
        "request_context": _RequestContext(),
        "manager": SimpleNamespace(manager_id="manager_1"),
        "commands": _Commands(),
        "progress": _Progress(),
        "clarification": _Clarification(),
        "schedules": _Schedules(),
        "deferred_delivery": SimpleNamespace(),
        "submission": SimpleNamespace(),
        "access_reviews": SimpleNamespace(),
    }
    return dependencies, delivery_repository


def test_runtime_application_exposes_narrow_session_and_config_views() -> None:
    dependencies, _ = _application_dependencies()
    application = GatewayRuntimeApplication(**dependencies)

    session = application.get_active_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )

    assert isinstance(application, GatewayApplication)
    assert not isinstance(application, WebsiteGatewayApplication)
    assert application.default_session_key == "default_session"
    assert session is not None
    assert session.session_key == "session_1"
    assert session.conversation_id == "conversation_1"
    assert application.is_task_progress_status_request("?") is True


@pytest.mark.asyncio
async def test_runtime_application_normalizes_queries_and_schedule_models() -> None:
    dependencies, _ = _application_dependencies()
    application = GatewayRuntimeApplication(**dependencies)

    assert await application.waiting_question_id("session_with_query") == "task_query_1"
    assert await application.waiting_question_id("session_without_query") is None

    jobs = await application.list_scheduled_jobs(session_key="session_1")

    assert len(jobs) == 1
    assert jobs[0].scheduled_job_id == "friendly_schedule_id"
    assert jobs[0].next_fire_at == "2026-07-27T00:00:00+00:00"
    assert jobs[0].task_summary == "Send the report"
