"""Task controls retain browser identity, CSRF protection and bounded reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_application import (
    WebTaskListRequestV1,
    WebTaskControlRequestV1,
)
from assistant_gateway.application.website_tasks import QUEUE_CHANGED_TEXT, list_tasks, control_tasks
from assistant_gateway.store import GatewayStateStore
from test_gateway_website_app import (
    _config,
    _UserTokenStatefulFakeGateway,
    InternalWebsiteInteractiveApp,
)


class _TeamCommands:
    """Stands in for the team services: records each command and answers with ``replies[kind]``."""

    def __init__(self, replies: dict) -> None:
        self.replies = replies
        self.sent: list[tuple[str, dict, str]] = []

    async def request(self, kind: str, body: dict, *, person_id: str) -> dict:
        self.sent.append((kind, body, person_id))
        return self.replies[kind]


@pytest.mark.asyncio
async def test_task_service_projects_the_agents_reply_and_forwards_exact_removal():
    context = GatewayRouteContextV1(
        session_key="session",
        canonical_user_id="owner",
        channel_name="website",
        user_id="user",
        chat_id="web-chat",
    )
    team = _TeamCommands({
        "tasks.list": {"outcome": "ok", "tasks": [{"task_id": "root", "state": "in_progress",
                                                    "problem_statement": "Current task", "is_current": True,
                                                    "is_parallel": True}],
                       "total": 1, "queued_count": 0, "current_count": 1, "queue_digest": "a" * 64},
        "tasks.remove": {"outcome": "ok", "receipts": [{"root_task_id": "root", "cancelled_task_count": 1,
                                                        "stop_signals_sent": 1, "unconfirmed_worker_count": 0,
                                                        "state": "confirmed"}],
                         "removed": 1, "queue_digest": "b" * 64, "all_confirmed": True},
        "tasks.purge": {"outcome": "queue_changed", "current_digest": "c" * 64, "message": "Changed."},
        "tasks.list_finished": {"outcome": "ok", "tasks": [{"task_id": "done", "state": "completed",
                                                             "problem_statement": "Overnight work",
                                                             "owner_agent_id": "stopped", "answer": "Result"}],
                                "total": 1},
    })
    result = await list_tasks(team, WebTaskListRequestV1(context=context, agent_id="worker", offset=20))
    assert result.tasks[0].is_current and result.tasks[0].is_parallel and result.current_count == 1
    assert result.inventory_digest == "a" * 64
    assert team.sent[-1] == ("tasks.list", {"agent": "worker", "offset": 20, "limit": 20}, "owner")
    removed = await control_tasks(team, WebTaskControlRequestV1(
        context=context, agent_id="worker", action="remove", task_id="root", inventory_digest="a" * 64))
    kind, body, _person = team.sent[-1]
    assert (kind, body["task_id"], body["seen_queue_digest"]) == ("tasks.remove", "root", "a" * 64)
    assert removed.success and removed.cancellation_receipts[0].confirmed is True
    assert removed.message.startswith("Removed 1 task.")
    changed = await control_tasks(team, WebTaskControlRequestV1(
        context=context, agent_id="worker", action="purge", inventory_digest="a" * 64))
    assert (changed.success, changed.message) == (False, QUEUE_CHANGED_TEXT)
    history = await list_tasks(team, WebTaskListRequestV1(context=context, agent_id="worker", view="finished",
                                                          limit=10))
    assert (history.tasks[0].answer, history.tasks[0].owner_agent_id) == ("Result", "stopped")
    assert team.sent[-1][0] == "tasks.list_finished"
    with pytest.raises(ValueError, match="Choose a task"):
        await control_tasks(team, WebTaskControlRequestV1(
            context=context, agent_id="worker", action="remove", inventory_digest="a" * 64))


@pytest.mark.asyncio
async def test_task_http_identity_pagination_csrf_and_exact_inventory(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    session = "website:web-chat:tasks"
    store.create_session(
        session_key=session,
        conversation_id="conversation",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    store.create_session(
        session_key="other-session",
        conversation_id="private",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="other",
        chat_id="web-chat",
        owner_canonical_user_id="other",
    )
    reads, writes = [], []

    async def listing(**request):
        reads.append(request)
        return {
            "tasks": [],
            "total": 0,
            "queued_count": 0,
            "current_count": 0,
            "inventory_digest": "a" * 64,
        }

    async def control(**request):
        writes.append(request)
        return {"success": True, "message": "Removed", "cancellation_receipts": []}

    gateway.list_web_chat_tasks = listing
    gateway.control_web_chat_tasks = control
    website = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    async with TestClient(TestServer(website.build_app())) as client:
        assert (await client.get("/api/web-chat/tasks")).status == 401
        auth = await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})
        auth_body = await auth.json()
        from tests.loop_storage_support import record_loop_opens

        # The Tasks panel asks every 10 seconds per agent; its database work runs off the loop.
        with record_loop_opens(monkeypatch) as loop_opens:
            read = await client.get(
                "/api/web-chat/tasks",
                params={
                    "session_key": session,
                    "agent_id": "worker",
                    "limit": 999,
                    "offset": 20,
                    "canonical_user_id": "other",
                },
            )
        assert loop_opens.where == [], loop_opens.report()
        assert read.status == 200
        assert reads[0]["canonical_user_id"] == "user_alpha"
        assert reads[0]["limit"] == 20
        assert reads[0]["offset"] == 20
        private = await client.get(
            "/api/web-chat/tasks", params={"session_key": "other-session"}
        )
        assert private.status in {403, 404}
        payload = {
            "session_key": session,
                "agent_id": "worker",
            "action": "purge",
            "inventory_digest": "a" * 64,
        }
        assert (
            await client.post("/api/web-chat/tasks/control", json=payload)
        ).status == 403
        headers = {
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_body["permission_csrf_token"],
        }
        assert (
            await client.post(
                "/api/web-chat/tasks/control",
                headers=headers,
                json={**payload, "inventory_digest": "invalid"},
            )
        ).status == 409
        result = await client.post(
            "/api/web-chat/tasks/control", headers=headers, json=payload
        )
        assert result.status == 200
        assert len(writes) == 1
        assert writes[0]["canonical_user_id"] == "user_alpha"
        assert writes[0]["inventory_digest"] == "a" * 64


@pytest.mark.asyncio
async def test_task_agent_selection_requires_current_team_presence():
    from assistant_gateway.application.website_services import WebsiteOperationsApplication
    from assistant_gateway.application.website_application import WebTeamMemberListV1, WebTeamMemberV1

    member = WebTeamMemberV1(
        agent_id="worker", name="Worker", named_agent_type="Reviewer", is_team_lead=False,
        status={"key": "busy", "label": "Busy", "tone": "warning"}, last_seen_at="2026-09-21T00:00:00Z",
    )
    app = object.__new__(WebsiteOperationsApplication)
    app._team = SimpleNamespace(list_web_chat_team_members=lambda **_kwargs: WebTeamMemberListV1(members=(member,)))
    app._team_commands = SimpleNamespace(request=AsyncMock())
    context = GatewayRouteContextV1(session_key="session", canonical_user_id="owner",
                                         channel_name="website", user_id="owner", chat_id="web-chat")
    with pytest.raises(ValueError, match="no longer available"):
        await app.list_web_chat_tasks(WebTaskListRequestV1(context=context, agent_id="other-team"))
    with pytest.raises(ValueError, match="no longer available"):
        await app.control_web_chat_tasks(WebTaskControlRequestV1(context=context, agent_id="other-team",
                                         action="purge", inventory_digest="a" * 64))
    app._team_commands.request.assert_not_awaited()
    app._require_task_agent("worker")
