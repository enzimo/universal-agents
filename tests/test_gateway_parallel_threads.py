"""A thread started with Launch parallel thread marks every request sent in it.

The browser asks for a parallel thread when it creates the chat; the gateway
stores the mark on the session and puts it on each request it sends the team
lead (``GatewayContext.parallel_thread``), which then runs the request beside
its other work (custom_agents.execution_lanes).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from tests.test_gateway_task_steering import _config as steering_config
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp, _config as website_config


class _AnsweringTransport:
    """The manager: records each request and answers it at once."""

    def __init__(self) -> None:
        self.gateway: UserGateway | None = None
        self.durable: list[MessageEnvelope] = []

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        self.durable.append(envelope)
        assert self.gateway is not None
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=Task(
                    task_id=envelope.msg_content.task_id, task_type=MessageMetaType.RESPONSE, answer="Done."
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_agent_1", to_routing_path="team_steering_gateway", agent_signature=""
                ),
                msg_meta=envelope.msg_meta.model_copy(
                    update={"requester_type": RequesterType.USER_REQUESTER, "is_response_requested": False}
                ),
            )
        )


@pytest.mark.asyncio
async def test_every_request_of_a_parallel_thread_carries_the_mark_and_others_do_not(tmp_path: Path) -> None:
    transport = _AnsweringTransport()
    gateway = UserGateway(steering_config(tmp_path), manager_transport=transport)
    transport.gateway = gateway
    route = {"channel_name": "cli", "user_id": "user", "chat_id": "cli"}
    parallel = gateway.application.start_new_session(**route, parallel_thread=True)
    ordinary = gateway.application.start_new_session(**route)

    assert parallel.parallel_thread and not ordinary.parallel_thread
    assert gateway.session_repository.get_session(parallel.session_key).parallel_thread is True
    await gateway.application.send_user_message("First request", session_key=parallel.session_key, **route)
    await gateway.application.send_user_message(
        "Later request in the same thread", session_key=parallel.session_key, **route
    )
    await gateway.application.send_user_message("Ordinary request", session_key=ordinary.session_key, **route)

    marks = [
        (envelope.msg_meta.gateway_context.parallel_thread, envelope.msg_meta.gateway_context.delivery_mode)
        for envelope in transport.durable
    ]
    assert marks == [(True, "interactive"), (True, "interactive"), (False, "interactive")]


@pytest.mark.asyncio
async def test_web_chat_creates_a_parallel_thread_only_when_asked(tmp_path: Path, monkeypatch) -> None:
    config = website_config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_parallel_test")
    gateway = UserGateway(config, output_sink=None, state_store=GatewayStateStore(str(tmp_path / "gateway.db")))
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        await client.post("/api/web-chat/auth", json={"token": "uaweb_parallel_test"})
        parallel = await client.post("/api/web-chat/sessions", json={"parallel_thread": True})
        new_chat = await client.post("/api/web-chat/sessions")
        refused = await client.post("/api/web-chat/sessions", json={"parallel_thread": "yes"})

        assert parallel.status == 201 and new_chat.status == 201
        parallel_session = (await parallel.json())["session"]
        assert parallel_session["parallel_thread"] is True
        assert (await new_chat.json())["session"]["parallel_thread"] is False
        assert refused.status == 400
        stored = gateway.session_repository.get_session(parallel_session["session_key"])
        assert stored is not None and stored.parallel_thread is True
