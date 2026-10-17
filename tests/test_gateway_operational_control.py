from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from assistant_gateway.application.operational_control import (
    GatewayOperationalControlApplication,
    GatewayOperationalControlSettings,
    GatewayStopControlRequest,
)
from assistant_gateway.application.request_context import (
    GatewayCommandPerson,
    GatewayRequesterPresentation,
    GatewayRequestRoute,
)
from assistant_gateway.application.objective_control_text import objective_control_text
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand, RuntimeWorkCancellationReceipt, RuntimeWorkSummary, SystemStatusMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.enums import ObjectiveStatus, ProgressState


class _FixedIds:
    def new_correlation_id(self) -> str:
        return "corr_control"


class _FakeSessions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.session = SessionRecord(
            session_key="session_control",
            session_alias=None,
            conversation_id="conv_control",
            last_task_id="task_previous",
            manager_agent_id="manager_1",
            channel_name="website",
            user_id="browser_user",
            chat_id="web-chat",
            thread_id="thread_1",
            status="active",
            created_at="2026-08-17T00:00:00+00:00",
            updated_at="2026-08-17T00:00:00+00:00",
            owner_canonical_user_id="usr_owner",
        )

    def resolve_session(self, **kwargs) -> SessionRecord:
        self.calls.append(kwargs)
        return self.session


class _FakeRequestContext:
    users_enabled = True

    def canonical_user_id(self, route: GatewayRequestRoute) -> str | None:
        return route.canonical_user_id or "usr_resolved"

    def requester_presentation_for_session(
        self,
        _session: SessionRecord,
        _route: GatewayRequestRoute,
    ) -> GatewayRequesterPresentation:
        return GatewayRequesterPresentation(
            canonical_user_id="usr_owner",
            display_name="Owner",
            timezone="America/Chicago",
        )

    def manager_principal(
        self,
        route: GatewayRequestRoute,
    ) -> GatewayCommandPerson:
        return GatewayCommandPerson(
            canonical_user_id=route.canonical_user_id or "usr_owner",
            team_id="test_team",
            roles=("member",),
        )

    def build_gateway_context(
        self,
        *,
        session_key: str,
        delivery_mode: str,
        route: GatewayRequestRoute,
        manager_agent_id: str | None,
        scheduled_job_id: str | None = None,
    ) -> GatewayContext:
        return GatewayContext(
            gateway_instance_name="test",
            session_key=session_key,
            channel_name=route.channel_name,
            user_id=route.user_id,
            canonical_user_id=route.canonical_user_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            delivery_mode=delivery_mode,
            scheduled_job_id=scheduled_job_id,
            schedule_owner_agent_id=manager_agent_id,
        )


class _FakeRequests:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.statuses = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)

    def update_status(self, correlation_id, status):
        self.statuses.append((correlation_id, status))


class _FakeManager:
    def __init__(self, manager_id: str | None = "manager_1") -> None:
        self.manager_id = manager_id
        self.open_channels: set[str] = set()
        self.published: list[MessageEnvelope] = []

    @asynccontextmanager
    async def response_channel(self, correlation_id: str):
        self.open_channels.add(correlation_id)
        try:
            yield SimpleNamespace()
        finally:
            self.open_channels.remove(correlation_id)

    async def publish_operational(self, envelope: MessageEnvelope) -> None:
        self.published.append(envelope)


class _FakeResponses:
    def __init__(self) -> None:
        self.target = ("task_active", "corr_active")
        self.target_requests: list[dict[str, object]] = []
        self.unavailable: list[str] = []
        self.awaited: list[dict[str, object]] = []

    async def resolve_target(self, **kwargs):
        self.target_requests.append(kwargs)
        return self.target

    async def queue_manager_unavailable(self, correlation_id: str) -> None:
        self.unavailable.append(correlation_id)

    async def await_terminal_response(self, **kwargs):
        self.awaited.append(kwargs)
        envelope = MessageEnvelope(
            msg_content=SystemStatusMessage(system_status="control applied"),
            msg_meta=MessageMeta(msg_correlation_id="corr_control"),
            msg_header=MessageHeader(from_agent_id="manager_1"),
        )
        return SimpleNamespace(
            correlation_id="corr_control",
            conversation_id="conv_control",
            task_id=None,
            message_type="system_status",
            text="control applied",
            raw_envelope=envelope,
            attachments=(),
        )


class _FakeManagerCommands:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def request(self, request):
        self.requests.append(request)
        command = request.command
        return command.model_copy(
            update={
                "roots": [
                    RuntimeWorkSummary(
                        root_task_id="task_active",
                        state=ProgressState.IN_PROGRESS,
                        session_key="session_control",
                        conversation_id="conv_control",
                    )
                ]
            }
        )


class _UnavailableManagerCommands:
    async def request(self, _request):
        raise RuntimeError("manager command route unavailable")


def _application(
    *,
    manager_id: str | None = "manager_1",
) -> tuple[
    GatewayOperationalControlApplication,
    _FakeSessions,
    _FakeRequests,
    _FakeManager,
    _FakeResponses,
]:
    sessions = _FakeSessions()
    requests = _FakeRequests()
    manager = _FakeManager(manager_id)
    responses = _FakeResponses()
    manager_commands = _FakeManagerCommands()
    application = GatewayOperationalControlApplication(
        settings=GatewayOperationalControlSettings(
            response_route="gateway_response",
            manager_named_agent_type="TeamArchitect",
            default_session_key="default",
            default_owner_canonical_user_id="usr_default",
        ),
        sessions=sessions,  # type: ignore[arg-type]
        request_context=_FakeRequestContext(),  # type: ignore[arg-type]
        requests=requests,  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        manager_commands=manager_commands,  # type: ignore[arg-type]
        responses=responses,  # type: ignore[arg-type]
        id_generator=_FixedIds(),  # type: ignore[arg-type]
    )
    return application, sessions, requests, manager, responses


@pytest.mark.asyncio
async def test_stop_control_owns_targeting_persistence_and_operational_exchange() -> None:
    application, sessions, requests, manager, responses = _application()

    response = await application.stop(
        GatewayStopControlRequest(
            session_key="session_control",
            route=GatewayRequestRoute(
                channel_name="website",
                user_id="browser_user",
                chat_id="web-chat",
                thread_id="thread_1",
                canonical_user_id="usr_owner",
            ),
        )
    )

    assert response.text.startswith("Active work:\n- task_active: in progress")
    assert manager.open_channels == set()
    assert sessions.calls[0]["owner_canonical_user_id"] == "usr_owner"
    assert sessions.calls[0]["enforce_owner_scope"] is True
    assert responses.target_requests == []
    assert requests.records == [
        {
            "correlation_id": "corr_control",
            "session_key": "session_control",
            "request_kind": "interactive_control",
            "request_text": "/stop",
            "conversation_id": "conv_control",
            "followup_to_task_id": None,
        }
    ]
    assert manager.published == []
    manager_commands = application._manager_commands
    assert len(manager_commands.requests) == 1
    command = manager_commands.requests[0].command
    assert isinstance(command, ManagerRuntimeWorkCommand)
    assert command.action == "inventory"
    assert command.scope == "user"
    # The command names nobody; the request carries the signed-in person for
    # the gateway's own checks and the signed context.
    assert manager_commands.requests[0].person.canonical_user_id == "usr_owner"
    assert "principal" not in ManagerRuntimeWorkCommand.model_fields


@pytest.mark.asyncio
async def test_operational_control_opens_fallback_response_channel_when_command_fails() -> None:
    application, _sessions, _requests, manager, responses = _application()
    application._manager_commands = _UnavailableManagerCommands()  # type: ignore[assignment]

    response = await application.stop(
        GatewayStopControlRequest(session_key="session_control")
    )

    assert response.text == "control applied"
    assert manager.open_channels == set()
    assert responses.unavailable == ["corr_control"]
    assert responses.awaited == [
        {
            "correlation_id": "corr_control",
            "session_key": "session_control",
            "query_handler": None,
        }
    ]


@pytest.mark.asyncio
async def test_control_timeout_reports_unconfirmed_outcome_without_claiming_disconnect():
    from unittest.mock import AsyncMock

    application, _sessions, requests, manager, responses = _application()
    application._manager_commands = SimpleNamespace(request=AsyncMock(side_effect=TimeoutError()))
    result = await application.stop(GatewayStopControlRequest(
        session_key="session_control", action="cancel", scope="user"))
    assert "not confirmed" in result.text
    assert "may still be processing" in result.text
    assert "reconnect" not in result.text
    assert responses.unavailable == []
    assert manager.manager_id == "manager_1"
    assert requests.statuses == [("corr_control", "timed_out")]


def test_cancellation_render_exposes_missing_restart_barrier() -> None:
    response = ManagerRuntimeWorkCommand(
        action="cancel",
        scope="task",
        success=False,
        cancellation_receipts=[
            RuntimeWorkCancellationReceipt(
                root_task_id="task_degraded",
                confirmed=False,
                cancelled_task_count=1,
                message=(
                    "Execution graph cancelled locally, but its restart barrier "
                    "could not be persisted."
                ),
            )
        ],
    )

    rendered = GatewayOperationalControlApplication._render_runtime_work_response(
        response
    )

    assert "task_degraded: not confirmed" in rendered
    assert "restart barrier could not be persisted" in rendered
    assert "Control incomplete:" in rendered


@pytest.mark.parametrize(
    ("item_count", "objective_ids", "expected"),
    [
        (5, ["objective_a", "objective_a"], "5 scheduled items and 1 scheduled objective"),
    ],
)
def test_scheduled_work_summary_is_compact(item_count, objective_ids, expected) -> None:
    from custom_messages.contracts.control_v1 import RuntimeWorkScheduleSummary

    response = ManagerRuntimeWorkCommand(
        action="cancel",
        scope="user",
        schedules=[
            RuntimeWorkScheduleSummary(
                schedule_id=f"schedule_{index}",
                schedule_name="A job name that belongs in the dashboard",
                objective_id=objective_id,
            )
            for index, objective_id in enumerate([None] * item_count + objective_ids)
        ],
    )

    rendered = GatewayOperationalControlApplication._render_runtime_work_response(
        response, review_url="/webchat?view=objectives"
    )

    assert f"Scheduled work: {expected}." in rendered
    assert "Future scheduled work is separate and was not stopped." in rendered
    assert "[Review schedules and objectives](/webchat?view=objectives)" in rendered
    assert "A job name" not in rendered
    assert "schedule_" not in rendered
    assert len(rendered.splitlines()) <= 5
    channel_text = GatewayOperationalControlApplication._render_runtime_work_response(response)
    assert "/schedules" in channel_text
    assert "/webchat" not in channel_text


def test_a_refused_objective_control_is_explained_and_not_reported_as_done() -> None:
    """A refusal changed nothing: no "is now" line, and the reason in plain words."""
    response = ManagerRuntimeWorkCommand(
        action="resume_objective",
        scope="objective",
        target_objective_id="objective_1",
        success=False,
        message="objective_action_unavailable:satisfied",
    )

    rendered = GatewayOperationalControlApplication._render_runtime_work_response(response)

    assert rendered.splitlines()[0] == (
        "Objective objective_1 is satisfied, so it can't be resumed. Nothing was changed."
    )
    assert "is now" not in rendered
    assert "objective_action_unavailable" not in rendered


@pytest.mark.parametrize(
    ("action", "message", "status", "expected"),
    [
        (
            "pause_objective",
            "objective_control_incomplete:schedule:sched_1,policy:policy_1",
            ObjectiveStatus.PAUSED,
            "Not every part finished: schedule sched_1, work-session setting policy_1. "
            "Send /pause objective objective_1 again to retry them.",
        ),
        (
            "delete_objective",
            "objective_deletion_incomplete:task:task_1,objective:objective_1",
            ObjectiveStatus.DELETING,
            "Objective objective_1 is still being deleted; these did not finish: task (its stop was "
            "not confirmed) task_1, recording which links it changed. Send /delete objective "
            "objective_1 again to finish it.",
        ),
        (
            "pause_objective",
            "objective_changed",
            None,
            "Objective objective_1 changed since you last looked at it, so nothing was done. "
            "Check /objectives and try again.",
        ),
    ],
)
def test_objective_control_outcomes_are_worded_for_people(action, message, status, expected) -> None:
    response = ManagerRuntimeWorkCommand(
        action=action,
        scope="objective",
        target_objective_id="objective_1",
        success=False,
        message=message,
        objective_status=status,
    )

    rendered = GatewayOperationalControlApplication._render_runtime_work_response(response)

    assert expected in rendered.splitlines()
    assert "Control incomplete:" not in rendered


def test_the_web_page_shows_a_finished_objective_control_in_words() -> None:
    response = ManagerRuntimeWorkCommand(
        action="pause_objective",
        scope="objective",
        target_objective_id="objective_1",
        success=True,
        message="objective_paused",
        objective_status=ObjectiveStatus.PAUSED,
    )

    assert objective_control_text(response) == "Objective objective_1 is now paused."
