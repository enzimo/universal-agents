from assistant_gateway.runtime_config import build_collector_runtime_settings
import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from activity_collector import service as service_module
from activity_collector.service import ACTIVITY_COLLECTOR_HTTP_MAX_BODY_BYTES
from activity_collector.service import ActivityCollectorService
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.activity_v1 import TaskActivityEvent
from custom_messages.enums import TaskActivityStage, TaskActivityType


@pytest.fixture
def collector_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIVITY_COLLECTOR_USER_TOKEN", "user-token")
    monkeypatch.setenv("ACTIVITY_COLLECTOR_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", "ingest-token")
    config = GatewayInstanceConfig.create(
        instance_name="collector-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Los_Angeles",
    )
    config.activity_collector.storage.sqlite.path = str(tmp_path / "instance" / "activity_collector.db")
    config.state.storage.sqlite.path = str(tmp_path / "instance" / "manager_state.db")
    config.activity_collector.otel_trace_base_url = "https://otel.example/traces/{trace_id}"
    return config


def _event(
    *,
    event_id: str,
    activity_type: TaskActivityType,
    stage: TaskActivityStage,
    summary: str,
    created_at: str,
    sequence: int = 1,
    user_visible: bool = True,
    detail: str | None = None,
    objective_id: str | None = None,
    objective_title: str | None = None,
    details: dict | None = None,
) -> TaskActivityEvent:
    return TaskActivityEvent(
        activity_event_id=event_id,
        created_at=created_at,
        sequence=sequence,
        team_id="team_test",
        manager_agent_id="manager-1",
        manager_name="Manager One",
        top_level_task_id="task-top-1",
        task_id="task-top-1",
        objective_id=objective_id,
        objective_title=objective_title,
        agent_id="worker-1",
        agent_name="Worker One",
        named_agent_type="WebResearchAgent",
        activity_type=activity_type,
        stage=stage,
        summary=summary,
        detail=detail,
        user_visible=user_visible,
        trace_id="trace-1234",
        details=details or {},
    )


def test_gateway_instance_activity_and_otel_defaults(collector_config: GatewayInstanceConfig) -> None:
    assert collector_config.activity_collector.enabled is True
    assert collector_config.otel.enabled is False
    assert collector_config.otel.enable_traces is False
    assert collector_config.otel.enable_metrics is False
    assert collector_config.activity_collector_storage_settings.sqlite_path.endswith("activity_collector.db")
    assert collector_config.activity_collector.resolve_user_token() == "user-token"
    assert collector_config.activity_collector.resolve_admin_token() == "admin-token"
    assert collector_config.activity_collector.resolve_ingest_token() == "ingest-token"


def test_activity_collector_requires_env_tokens(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ACTIVITY_COLLECTOR_USER_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_USER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_ADMIN_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE", raising=False)
    config = GatewayInstanceConfig.create(
        instance_name="collector-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Los_Angeles",
    )

    with pytest.raises(ValueError, match="ACTIVITY_COLLECTOR_USER_TOKEN"):
        ActivityCollectorService(build_collector_runtime_settings(config), logger=logging.getLogger("test.activity.collector.missing_tokens"))


def test_activity_collector_requires_ingest_env_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACTIVITY_COLLECTOR_USER_TOKEN", "user-token")
    monkeypatch.setenv("ACTIVITY_COLLECTOR_ADMIN_TOKEN", "admin-token")
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE", raising=False)
    config = GatewayInstanceConfig.create(
        instance_name="collector-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Los_Angeles",
    )

    with pytest.raises(ValueError, match="ACTIVITY_COLLECTOR_INGEST_TOKEN"):
        ActivityCollectorService(build_collector_runtime_settings(config), logger=logging.getLogger("test.activity.collector.missing_ingest_token"))


def test_activity_collector_dashboard_html_escapes_untrusted_fields(
    collector_config: GatewayInstanceConfig,
) -> None:
    service = ActivityCollectorService(build_collector_runtime_settings(collector_config), logger=logging.getLogger("test.activity.collector.html"))

    html = service._render_html(  # noqa: SLF001 - focused dashboard XSS regression.
        title="Dashboard",
        summary_endpoint="/api/user/team-summary",
        stream_endpoint="/api/stream/user",
        mode="user",
    )

    assert "function escapeHtml" in html
    assert "function safeTraceLink" in html
    assert "${escapeHtml(item.summary)}" in html
    assert "${safeTraceLink(item.trace_url)}" in html
    assert 'href="${item.trace_url}"' not in html


@pytest.mark.asyncio
async def test_run_activity_collector_disables_aiohttp_access_log(
    collector_config: GatewayInstanceConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Mock()
    runner.setup = AsyncMock()
    runner.cleanup = AsyncMock()
    app_runner = Mock(return_value=runner)
    monkeypatch.setattr(service_module.web, "AppRunner", app_runner)

    site = Mock()
    site.start = AsyncMock()
    monkeypatch.setattr(service_module.web, "TCPSite", Mock(return_value=site))

    stop_event = Mock()
    stop_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(service_module.asyncio, "Event", Mock(return_value=stop_event))

    with pytest.raises(asyncio.CancelledError):
        await service_module.run_activity_collector(build_collector_runtime_settings(collector_config))

    assert app_runner.call_args.kwargs["access_log"] is None
    runner.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_collector_user_and_admin_views(collector_config: GatewayInstanceConfig) -> None:
    service = ActivityCollectorService(build_collector_runtime_settings(collector_config), logger=logging.getLogger("test.activity.collector"))

    server = TestServer(service.build_app())
    client = TestClient(server)
    await client.start_server()
    try:
        events = [
            _event(
                event_id="evt-1",
                activity_type=TaskActivityType.DELEGATED,
                stage=TaskActivityStage.DELEGATING,
                summary="Subtask created and sent to WebResearchAgent.",
                detail="Search the web for current docs.",
                created_at="2026-04-22T12:00:00+00:00",
                sequence=1,
                objective_id="objective_id_docs",
                objective_title="Research docs",
            ),
            _event(
                event_id="evt-2",
                activity_type=TaskActivityType.WAITING_ON_USER,
                stage=TaskActivityStage.WAITING,
                summary="Waiting on clarification from the user.",
                detail="Need a target API version.",
                created_at="2026-04-22T12:01:00+00:00",
                sequence=2,
                objective_id="objective_id_docs",
                objective_title="Research docs",
            ),
            _event(
                event_id="evt-3",
                activity_type=TaskActivityType.RESUMED,
                stage=TaskActivityStage.RUNNING,
                summary="Clarification received; work resumed.",
                detail="The target API version is v2.",
                created_at="2026-04-22T12:02:00+00:00",
                sequence=3,
                objective_id="objective_id_docs",
                objective_title="Research docs",
            ),
            _event(
                event_id="evt-4",
                activity_type=TaskActivityType.WORKER_RECLAIMED,
                stage=TaskActivityStage.RUNNING,
                summary="Recovered work from stale worker runtime state.",
                detail="Worker reclaimed task state after a restart.",
                created_at="2026-04-22T12:03:00+00:00",
                sequence=4,
                user_visible=False,
            ),
        ]
        missing_token_response = await client.post(
            "/api/activity/batch",
            json={"events": [events[0].model_dump(mode="json")]},
        )
        bad_token_response = await client.post(
            "/api/activity/batch",
            headers={"X-Activity-Collector-Token": "wrong-token"},
            json={"events": [events[0].model_dump(mode="json")]},
        )
        shared_header_response = await client.post(
            "/api/activity/batch",
            headers={"X-Shared-Token": "ingest-token"},
            json={"events": [events[0].model_dump(mode="json")]},
        )
        dedicated_header_response = await client.post(
            "/api/activity/batch",
            headers={"X-Activity-Collector-Token": "ingest-token"},
            json={"events": []},
        )
        response = await client.post(
            "/api/activity/batch",
            headers={"Authorization": "Bearer ingest-token"},
            json={"events": [event.model_dump(mode="json") for event in events]},
        )
        dedicated_header_payload = await dedicated_header_response.json()
        payload = await response.json()
        assert missing_token_response.status == 401
        assert bad_token_response.status == 401
        assert shared_header_response.status == 401
        assert dedicated_header_response.status == 202
        assert dedicated_header_payload["inserted"] == 0
        assert response.status == 202
        assert payload["inserted"] == 4

        user_summary = await client.get("/api/user/team-summary", params={"token": "user-token"})
        user_payload = await user_summary.json()
        assert user_summary.status == 200
        assert user_payload["team"]["manager_name"] == "Manager One"
        assert user_payload["team"]["team_composition"]["TeamArchitect"] == 1
        assert user_payload["team"]["team_composition"]["WebResearchAgent"] == 1
        assert user_payload["objectives"][0]["objective_id"] == "objective_id_docs"
        assert user_payload["objectives"][0]["objective_title"] == "Research docs"
        assert user_payload["tasks_in_progress"][0]["current_summary"] == "Clarification received; work resumed."
        assert user_payload["tasks_in_progress"][0]["objective_id"] == "objective_id_docs"
        assert any(
            item["summary"] == "Subtask created and sent to WebResearchAgent."
            for item in user_payload["recent_activity"]
        )
        user_notice = next(
            item
            for item in user_payload["recent_activity"]
            if item["summary"] == "Subtask created and sent to WebResearchAgent."
        )
        assert user_notice["activity_event_id"] == "evt-1"
        assert user_notice["objective_title"] == "Research docs"
        assert "details" in user_notice
        assert user_notice["user_visible"] is True

        admin_agents = await client.get("/api/admin/agents", params={"token": "admin-token"})
        admin_agents_payload = await admin_agents.json()
        assert admin_agents.status == 200
        assert {agent["agent_id"] for agent in admin_agents_payload["agents"]} == {"manager-1", "worker-1"}

        admin_tasks = await client.get("/api/admin/tasks", params={"token": "admin-token"})
        admin_tasks_payload = await admin_tasks.json()
        assert admin_tasks_payload["tasks"][0]["events"][-1]["trace_url"] == "https://otel.example/traces/trace-1234"

        admin_objectives = await client.get("/api/admin/objectives", params={"token": "admin-token"})
        admin_objectives_payload = await admin_objectives.json()
        assert admin_objectives_payload["objectives"][0]["objective_id"] == "objective_id_docs"

        admin_health = await client.get("/api/admin/health", params={"token": "admin-token"})
        admin_health_payload = await admin_health.json()
        assert admin_health_payload["repository"]["total_events"] == 4
        assert admin_health_payload["collector"]["ingested_events"] == 4
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_activity_collector_rejects_oversized_ingest_batch(
    collector_config: GatewayInstanceConfig,
) -> None:
    collector_config.activity_collector.batch_max_events = 1
    service = ActivityCollectorService(build_collector_runtime_settings(collector_config), logger=logging.getLogger("test.activity.collector.batch_cap"))

    server = TestServer(service.build_app())
    client = TestClient(server)
    await client.start_server()
    try:
        events = [
            _event(
                event_id="evt-cap-1",
                activity_type=TaskActivityType.DELEGATED,
                stage=TaskActivityStage.DELEGATING,
                summary="One.",
                created_at="2026-04-22T12:00:00+00:00",
            ),
            _event(
                event_id="evt-cap-2",
                activity_type=TaskActivityType.DELEGATED,
                stage=TaskActivityStage.DELEGATING,
                summary="Two.",
                created_at="2026-04-22T12:00:01+00:00",
            ),
        ]
        response = await client.post(
            "/api/activity/batch",
            headers={"Authorization": "Bearer ingest-token"},
            json={"events": [event.model_dump(mode="json") for event in events]},
        )

        assert response.status == 413
        assert "batch exceeds limit of 1" in await response.text()
        assert service._repository.list_recent_events(team_id="team_test") == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_activity_collector_rejects_oversized_http_body(
    collector_config: GatewayInstanceConfig,
) -> None:
    service = ActivityCollectorService(build_collector_runtime_settings(collector_config), logger=logging.getLogger("test.activity.collector.body_cap"))

    server = TestServer(service.build_app())
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post(
            "/api/activity/batch",
            headers={"Authorization": "Bearer ingest-token", "Content-Type": "application/json"},
            data='{"events":[],"padding":"' + ("x" * ACTIVITY_COLLECTOR_HTTP_MAX_BODY_BYTES) + '"}',
        )

        assert response.status == 413
        assert service._repository.list_recent_events(team_id="team_test") == []
    finally:
        await client.close()
