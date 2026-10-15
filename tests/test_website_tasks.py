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
from assistant_gateway.application.website_tasks import list_tasks, control_tasks
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.control_v1 import (
    FinishedTaskSummary,
    ManagerRuntimeWorkCommand,
    RuntimeWorkSummary,
)
from custom_messages.enums import ProgressState
from test_gateway_website_app import (
    _config,
    _UserTokenStatefulFakeGateway,
    InternalWebsiteInteractiveApp,
)


@pytest.mark.parametrize("action,expected", [("list_tasks", "private.read"), ("list_finished_tasks", "private.read"), ("remove_task", "private.delete"), ("purge_tasks", "private.delete")])
def test_task_manager_commands_authorize_the_actual_operation(action, expected):
    from assistant_gateway.application.manager_commands import GatewayManagerCommandService

    command = ManagerRuntimeWorkCommand(
        action=action, scope="user", target_task_id="root" if action == "remove_task" else None,
    )
    from assistant_gateway.application.request_context import GatewayCommandPerson

    target = GatewayManagerCommandService._private_resource_target(
        command,
        session=SimpleNamespace(),
        person=GatewayCommandPerson(canonical_user_id="owner", team_id="team", roles=("member",)),
    )
    assert target.resource_type == "Task"
    assert target.action == expected
    assert target.resource_id == ("root" if action == "remove_task" else "user:owner")


@pytest.mark.asyncio
async def test_task_service_projects_manager_state_and_forwards_exact_removal():
    context = GatewayRouteContextV1(
        session_key="session",
        canonical_user_id="owner",
        channel_name="website",
        user_id="user",
        chat_id="web-chat",
    )
    response = ManagerRuntimeWorkCommand(
        action="list_tasks",
        scope="user",
        success=True,
        inventory_digest="a" * 64,
        task_total=1,
        current_task_count=1,
        roots=[
            RuntimeWorkSummary(
                root_task_id="root",
                state=ProgressState.IN_PROGRESS,
                problem_statement="Current task",
                is_current=True,
            )
        ],
    )
    controls = SimpleNamespace(
        stop=AsyncMock(
            return_value=SimpleNamespace(
                raw_envelope=SimpleNamespace(msg_content=response)
            )
        )
    )
    result = await list_tasks(
        controls, WebTaskListRequestV1(context=context, agent_id="worker", offset=20)
    )
    assert result.tasks[0].is_current
    assert result.current_count == 1
    sent = controls.stop.call_args.args[0]
    assert sent.action == "list_tasks"
    assert sent.task_offset == 20
    assert sent.route.canonical_user_id == "owner"
    await control_tasks(
        controls,
        WebTaskControlRequestV1(
            context=context, agent_id="worker", action="remove", task_id="root", inventory_digest="a" * 64
        ),
    )
    sent = controls.stop.call_args.args[0]
    assert sent.action == "remove_task"
    assert sent.target_task_id == "root"
    assert sent.inventory_digest == "a" * 64
    response.finished_tasks = [FinishedTaskSummary(task_id="done", state="completed", problem_statement="Overnight work", owner_agent_id="stopped", answer="Result")]
    response.inventory_digest = None
    history = await list_tasks(controls, WebTaskListRequestV1(context=context, agent_id="worker", view="finished", limit=10))
    assert history.tasks[0].answer == "Result"
    assert history.tasks[0].owner_agent_id == "stopped"
    assert controls.stop.call_args.args[0].action == "list_finished_tasks"
    with pytest.raises(ValueError, match="Choose a task"):
        await control_tasks(
            controls,
            WebTaskControlRequestV1(
                context=context, agent_id="worker", action="remove", inventory_digest="a" * 64
            ),
        )


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
    app._operational_controls = SimpleNamespace(stop=AsyncMock())
    context = GatewayRouteContextV1(session_key="session", canonical_user_id="owner",
                                         channel_name="website", user_id="owner", chat_id="web-chat")
    with pytest.raises(ValueError, match="no longer available"):
        await app.list_web_chat_tasks(WebTaskListRequestV1(context=context, agent_id="other-team"))
    with pytest.raises(ValueError, match="no longer available"):
        await app.control_web_chat_tasks(WebTaskControlRequestV1(context=context, agent_id="other-team",
                                         action="purge", inventory_digest="a" * 64))
    app._operational_controls.stop.assert_not_awaited()
    app._require_task_agent("worker")
