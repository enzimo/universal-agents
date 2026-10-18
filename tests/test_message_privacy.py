"""Private runtime details stay out of model inputs and outbound task replies."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
import test_activity_collector
import yaml
from aiohttp import encode_basic_auth, web
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.http_activity_source import HttpGatewayActivitySource
from assistant_gateway.runtime_config import build_collector_runtime_settings
from activity_collector.service import ActivityCollectorService
from custom_agents.instance_delivery_context import render_delivery_settings_context
from custom_agents.prompt_context_sanitization import render_incoming_message_summary
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType
from test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _config,
    _FakeGateway,
)

collector_config = test_activity_collector.collector_config


@pytest.mark.asyncio
async def test_agent_status_requires_sign_in_before_contacting_collector(tmp_path):
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    website = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )
    async with TestClient(TestServer(website.build_app())) as client:
        response = await client.get("/api/agents/status")
        assert response.status == 401


@pytest.mark.asyncio
async def test_collector_rejects_tokens_in_urls_and_supports_header_auth(
    collector_config,
):
    service = ActivityCollectorService(
        build_collector_runtime_settings(collector_config)
    )
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.get("/api/user/team-summary?token=user-token")
        assert response.status == 401
        response = await client.get("/user")
        assert response.status == 401
        assert response.headers["WWW-Authenticate"].startswith("Basic ")
        response = await client.get("/user", headers={"Authorization": encode_basic_auth("token", "user-token")})
        assert response.status == 200
        assert 'searchParams.set("token"' not in await response.text()
        response = await client.get(
            "/api/user/team-summary", headers={"Authorization": "Bearer user-token"}
        )
        assert response.status == 200
        response = await client.get(
            "/api/admin/agents", headers={"Authorization": encode_basic_auth("token", "user-token")}
        )
        assert response.status == 401


@pytest.mark.asyncio
async def test_collector_compares_header_tokens_in_constant_time(
    collector_config, monkeypatch
):
    import activity_collector.service as module

    compare = Mock(wraps=module.hmac.compare_digest)
    monkeypatch.setattr(module.hmac, "compare_digest", compare)
    service = ActivityCollectorService(
        build_collector_runtime_settings(collector_config)
    )
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.get(
            "/api/user/team-summary", headers={"Authorization": "Bearer user-token"}
        )
        assert response.status == 200
    compare.assert_called_once_with(b"user-token", b"user-token")


@pytest.mark.asyncio
async def test_gateway_activity_polling_never_puts_token_in_url():
    requests = []

    async def recent(request):
        requests.append((str(request.rel_url), request.headers.get("Authorization")))
        return web.json_response({"recent_activity": [{"summary": "Ready"}]})

    app = web.Application()
    app.router.add_get("/api/user/recent-activity", recent)
    async with TestClient(TestServer(app)) as server:
        source = HttpGatewayActivitySource(
            base_url=str(server.make_url("/")).rstrip("/"),
            token="user-token",
            connect_timeout_seconds=1,
            request_timeout_seconds=1,
        )
        try:
            assert await source.fetch_recent_activity() == [{"summary": "Ready"}]
        finally:
            await source.close()
    assert requests == [("/api/user/recent-activity", "Bearer user-token")]


def test_compose_keeps_broker_off_public_host_interfaces():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    assert "127.0.0.1:18600:18600" in compose["services"]["ua"]["ports"]


def test_delivery_prompts_do_not_disclose_channel_addresses(tmp_path):
    config = _config(tmp_path)
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "private-chat-id"
    config.delivery.default_target.thread_id = "private-thread-id"
    rendered = render_delivery_settings_context(config.delivery)
    assert "Default delivery channel: telegram" in rendered
    assert "private-chat-id" not in rendered
    assert "private-thread-id" not in rendered
    summary = render_incoming_message_summary(
        content=Task(task_type=MessageMetaType.REQUEST),
        header=None,
        meta=MessageMeta(
            gateway_context=GatewayContext(
                gateway_instance_name="private-instance",
                session_key="private-session",
                channel_name="telegram",
                chat_id="private-chat-id",
                original_message_id="private-message",
            )
        ),
    )
    assert "channel=telegram" in summary
    assert "private-" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_generated_failure_replies_do_not_copy_private_history(
    monkeypatch, paused
):
    import custom_agents.strands_agent as module
    from custom_messages.enums import RecoverablePauseTrigger

    agent = SimpleNamespace(
        id="lead",
        name="Lead",
        team_id="team_test",
        named_agent_type="TeamArchitect",
        named_agent_type_queue_map={},
        _agent_id_by_name={},
        _logger=logging.getLogger("test.reply.privacy"),
        _record_runtime_repair_anomaly=lambda *args, **kwargs: None,
        _extract_team_id_from_routing_value=lambda _: None,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    monkeypatch.setattr(
        module, "build_outgoing_meta", AsyncMock(return_value=MessageMeta())
    )
    monkeypatch.setattr(
        module,
        "construct_header",
        AsyncMock(return_value=MessageHeader(from_agent_id="lead", agent_signature="")),
    )
    monkeypatch.setattr(
        module, "resolve_outgoing_routing", AsyncMock(return_value="gateway")
    )
    task = Task(
        task_id="task_1",
        task_type=MessageMetaType.REQUEST,
        task_plan="Private runtime plan",
        execution_history=[
            TaskHistoryEntry(
                summary="Private unrelated history",
                details={"chat_id": "private-chat-id"},
            )
        ],
    )
    args = dict(
        task_request=task,
        msg_header=MessageHeader(from_agent_id="gateway", agent_signature=""),
        msg_meta=MessageMeta(),
        error_message="The provider refused this request.",
    )
    if paused:
        await module.UniversalAgent._send_paused_subtask_response(
            agent, **args, trigger=RecoverablePauseTrigger.PROVIDER_OVERLOADED
        )
    else:
        await module.UniversalAgent._send_task_error_response(agent, **args)
    reply = agent.team_comms.send_message.await_args.kwargs["message"].msg_content
    assert not reply.task_plan
    assert "Private" not in reply.model_dump_json()
    assert "private-chat-id" not in reply.model_dump_json()
    assert "provider refused" in reply.answer
