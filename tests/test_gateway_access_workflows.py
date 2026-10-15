"""The workflow page with the real access Inbox, Cedar and SQLite.

Covers: the page shows each step's linked requests with their current status
from the access store (a waiting one links to its Inbox review), and requests
not on a step; Review Ready Requests offers the Inbox's own choices; deciding
goes through the access Inbox (the agent is told, a denial closes its step);
the whole selection is checked before anything is decided; only the owner
sees the workflow; an ended task cannot be decided from a stale page; and the
HTTP routes through the real website app (sign-in, CSRF, assets, download).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.access_workflows_page import GatewayAccessWorkflowPage
from assistant_gateway.application.access_workflows import WorkflowDecisionsV2
from custom_agents.access_workflows import AccessWorkflowService, WorkflowRequester, WorkflowRequestWatcher
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestResolvedV1,
    ChangeProposalV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.access_workflow_v2 import WorkflowProgressV2, WorkflowProposalV2
from storage.access_repository import AccessRepository
from storage.access_workflow_repository import AccessWorkflowRepository
from tests.access_workflow_support import BINARY, needs_cedar, proposal, storage_settings

pytestmark = needs_cedar
TEAM = "team-1"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
MEMBER = PersonPrincipal("member-1", TEAM, ("member",))
LEAD = AgentPrincipal("agent-lead", "TeamArchitect", "team_lead", TEAM, work_id="request-1")
# work_id: the request both tasks work on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="request-1")
CONTEXT = "task_context_one"


class Identity:
    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active") if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


@dataclass
class Manager:
    connected: bool = True
    published: list[tuple[str, Any]] = field(default_factory=list)

    async def publish_durable(self, envelope: Any, *, routing_key: str) -> None:
        self.published.append((routing_key, envelope))


@dataclass
class Harness:
    page: GatewayAccessWorkflowPage
    agents: AccessRequests
    manager: Manager
    workflow_id: str
    change_request: Any
    worker_request: Any


def change(change_id: str = "generated_agent_crawler") -> ChangeProposalV1:
    return ChangeProposalV1(
        kind="agent_definition",
        change_id=change_id,
        plan_digest="a" * 64,
        title="Create the crawler specialist",
        summary="Create the specialist CrawlerAgent. Available as soon as it is created; no restart.",
        effect=("Creates the CrawlerAgent specialist.",),
    )


@pytest.fixture
def harness(tmp_path: Path):
    storage = storage_settings(tmp_path)
    manager = Manager()

    async def emit(_event: Any) -> None:
        return None

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type="TeamArchitect",
            inbox_url="/webchat",
            instance_root=None,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(Identity({"owner-1": "owner", "member-1": "member"})),
        manager_client=manager,
        session_repository=SimpleNamespace(get_session_by_conversation_id=lambda _id: None),
        proactive_session=lambda: None,
        emit_delivery_event=emit,
    )
    workflows = AccessWorkflowService(AccessWorkflowRepository(storage))
    record = workflows.submit(
        WorkflowProposalV2.model_validate(proposal()),
        requester=WorkflowRequester("agent-lead", "TeamArchitect", TEAM, OWNER.person_id, context_id=CONTEXT),
        task_id="task-lead",
        idempotency_key="weekly",
        catalog=load_catalog(None),
    )
    for step, state in (("account", "complete"), ("specialist", "running"), ("research", "running")):
        record = workflows.progress(record, WorkflowProgressV2(step_id=step, state=state, summary="ok"))
    # The agents' side: requests are linked as they are made (same signed context).
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
    )
    agents = AccessRequests(
        service=service,
        inbox=StoredAccessInbox(service.store),
        lead_agent_type="TeamArchitect",
        watcher=WorkflowRequestWatcher(lambda: workflows, lambda: CONTEXT),
    )
    change_request = agents.submit_change(
        requester=LEAD, person=OWNER, proposal=change(), reason="The plan needs it", task_id="task-lead"
    ).request
    worker_request = agents.submit(
        requester=WORKER, person=OWNER, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.WRITE, reason="Schedule the report", task_id="task-worker",
    ).request
    page = GatewayAccessWorkflowPage(service=AccessWorkflowService(AccessWorkflowRepository(storage)), access_inbox=inbox,
                                     team_id=TEAM)
    yield Harness(page, agents, manager, record.workflow_id, change_request, worker_request)
    inbox.close()
    service.cedar.close()


def read(harness: Harness, owner: str = OWNER.person_id) -> dict:
    return asyncio.run(harness.page.read(harness.workflow_id, owner_id=owner))


def step(payload: dict, step_id: str) -> dict:
    return next(item for item in payload["workflow"]["steps"] if item["step_id"] == step_id)


def decide(harness: Harness, *decisions: tuple[str, str]) -> dict:
    selection = WorkflowDecisionsV2.model_validate(
        {"decisions": [{"request_id": request_id, "choice": choice} for request_id, choice in decisions]}
    )
    return asyncio.run(harness.page.decide(harness.workflow_id, owner_id=OWNER.person_id, selection=selection))


def test_the_page_shows_each_steps_requests_with_their_current_status(harness: Harness) -> None:
    payload = read(harness)

    [attach] = step(payload, "specialist")["requests"]
    assert attach["request_id"] == harness.change_request.request_id and attach["outcome"] == "pending"
    assert attach["review_url"] == f"/webchat?view=inbox#access_request={harness.change_request.request_id}"
    [research] = step(payload, "research")["requests"]
    assert research["from_worker"] is True and research["agent_type"] == "WebResearchAgent"
    assert step(payload, "research")["review_status"] == "Waiting for 1 decision(s); 0 of 1 approved"
    assert payload["waiting_request_count"] == 2

    # Decided in the Inbox, not here: the page reads the current status.
    asyncio.run(harness.page.access_inbox.decide(OWNER.person_id, harness.worker_request.request_id, "approve_task"))
    research = step(read(harness), "research")
    assert research["requests"][0]["outcome"] == "approved" and "review_url" not in research["requests"][0]
    assert research["review_status"] == "All requests approved; the agent still has to finish and check the work"


def test_requests_not_on_a_step_are_listed_with_their_status(harness: Harness) -> None:
    extra = harness.agents.submit_change(
        requester=LEAD, person=OWNER, proposal=change("runtime_restart_now").model_copy(update={"kind": "runtime_restart"}),
        reason="Restart", task_id="task-lead",
    ).request
    payload = read(harness)
    assert [item["request_id"] for item in payload["requests_not_on_a_step"]] == [extra.request_id]
    assert payload["requests_not_on_a_step"][0]["review_url"].endswith(extra.request_id)


def test_review_ready_requests_offers_the_inbox_choices(harness: Harness) -> None:
    reviews = {item["request_id"]: item for item in asyncio.run(
        harness.page.prepare(harness.workflow_id, owner_id=OWNER.person_id))["reviews"]}

    staged = reviews[harness.change_request.request_id]
    assert [choice["choice"] for choice in staged["choices"]] == ["approve", "deny"]
    assert staged["step_id"] == "specialist" and staged["effect"] == ["Creates the CrawlerAgent specialist."]
    access = reviews[harness.worker_request.request_id]
    assert [choice["choice"] for choice in access["choices"]] == ["approve", "approve_read", "deny"]


def test_decisions_go_through_the_access_inbox_and_a_denial_closes_its_step(harness: Harness) -> None:
    result = decide(harness, (harness.change_request.request_id, "approve"), (harness.worker_request.request_id, "deny"))

    assert result["all_saved"] is True
    assert {item["review"]["status"] for item in result["results"]} == {"granted", "denied"}
    told = {envelope.msg_content.request_id for _route, envelope in harness.manager.published
            if isinstance(envelope.msg_content, AccessRequestResolvedV1)}
    assert told == {harness.change_request.request_id, harness.worker_request.request_id}
    payload = read(harness)
    assert step(payload, "research")["blocked_by_person"] is True
    assert step(payload, "research")["state"] == "failed"
    assert step(payload, "specialist")["requests"][0]["outcome"] == "approved"
    assert {event["code"] for event in payload["events"]} >= {"decision_recorded", "step_denied"}
    # The denied step is closed: its tool cannot be asked for again in this task tree.
    with pytest.raises(HandDownRefused, match="denied a request"):
        harness.agents.submit(
            requester=WORKER, person=OWNER, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
            level=AccessLevel.READ, reason="Try again", task_id="task-worker",
        )


def test_the_whole_selection_is_checked_before_anything_is_decided(harness: Harness) -> None:
    # No step asks for publishing, so this request is not on a step.
    other = harness.agents.submit(
        requester=WORKER, person=OWNER, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing"),
        level=AccessLevel.WRITE, reason="Elsewhere", task_id="task-elsewhere",
    ).request
    # Linked as not on a step? Only within the workflow's context, which it shares here;
    # a request of another workflow owner is never selectable.
    foreign = AccessRequests(service=harness.agents.service, inbox=harness.agents.inbox,
                             lead_agent_type="TeamArchitect").submit(
        requester=WORKER, person=MEMBER, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.WRITE, reason="Not this plan", task_id="task-member",
    ).request
    with pytest.raises(ValueError, match="no longer waiting in this workflow"):
        decide(harness, (harness.change_request.request_id, "approve"), (foreign.request_id, "approve"))
    with pytest.raises(ValueError, match="offered"):
        decide(harness, (harness.change_request.request_id, "approve_read"))
    store = harness.agents.service.store
    assert store.get_request(harness.change_request.request_id).status.value == "waiting", "nothing was decided"
    assert other.request_id in {item["request_id"] for item in read(harness)["requests_not_on_a_step"]}


def test_only_the_owner_sees_the_workflow_and_an_ended_task_cannot_be_decided(harness: Harness, monkeypatch) -> None:
    with pytest.raises(PermissionError, match="not found"):
        read(harness, owner=MEMBER.person_id)
    with pytest.raises(PermissionError):
        harness.page.diagnostics(harness.workflow_id, owner_id=MEMBER.person_id)
    monkeypatch.setattr(harness.page.service, "task_status", lambda _record: {"state": "failed"})
    with pytest.raises(ValueError, match="source task has ended"):
        asyncio.run(harness.page.prepare(harness.workflow_id, owner_id=OWNER.person_id))
    with pytest.raises(ValueError, match="source task has ended"):
        decide(harness, (harness.change_request.request_id, "approve"))


@pytest.mark.asyncio
async def test_workflow_routes_need_sign_in_and_csrf_and_serve_their_assets(tmp_path, monkeypatch) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.interactive_apps.manager import InteractiveAppManager
    from assistant_gateway.interactive_apps.website import InternalWebsiteInteractiveApp
    from assistant_gateway.config import GatewayInstanceConfig

    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "workflow_test_token")
    config = GatewayInstanceConfig.create(
        instance_name="workflow_gateway_test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_workflow_gateway",
        timezone="UTC",
        manager_agent_id="manager_workflow_gateway",
        response_queue_name="workflow_gateway_queue",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    config.authorization.cedar_binary_path = str(BINARY)
    gateway = UserGateway(config)
    owner = config.user.canonical_user_id
    record = gateway.application.workflows.service.submit(
        WorkflowProposalV2.model_validate(proposal()),
        requester=WorkflowRequester("manager", "TeamArchitect", config.manager.team_id, owner),
        task_id="task-http",
        idempotency_key="http",
        catalog=load_catalog(None),
    )
    try:
        config.interactive_apps.internal_website.web_chat_enabled = True
        config.save(config.config_path)
        manager = InteractiveAppManager(config, gateway.application, include_apps=["website"])
        website = InternalWebsiteInteractiveApp(
            config.interactive_apps.internal_website, gateway.application, runtime=manager.runtime
        )
        async with TestClient(TestServer(website.build_app())) as client:
            root = f"/api/web-chat/workflows/{record.workflow_id}"
            assert (await client.get(root)).status == 401
            auth = await client.post("/api/web-chat/auth", json={"token": "workflow_test_token"})
            assert auth.status == 200
            headers = {
                "Origin": str(client.make_url("/")).rstrip("/"),
                "X-UA-Permission-CSRF": (await auth.json())["permission_csrf_token"],
            }
            for asset in ("workflow.mjs", "workflow_graph.mjs", "workflow_card.mjs", "access_inbox.mjs", "workflow.css"):
                response = await client.get(f"/{asset}")
                assert response.status == 200, asset
                if asset.endswith(".mjs"):
                    assert "javascript" in response.content_type
            assert (await client.get(f"/workflows/{record.workflow_id}")).status == 200
            state = await client.get(root)
            assert state.status == 200
            body = await state.json()
            assert body["workflow"]["workflow_id"] == record.workflow_id and body["waiting_request_count"] == 0
            assert (await client.post(root + "/review", json={})).status == 403, "writes need the CSRF header"
            review = await client.post(root + "/review", headers=headers, json={})
            assert review.status == 200 and (await review.json())["reviews"] == []
            refused = await client.post(root + "/decisions", headers=headers,
                                        json={"decisions": [{"request_id": "access_req_missing", "choice": "approve"}]})
            assert refused.status in {400, 403, 409} and "no longer waiting" in (await refused.text())
            assert (await client.post(root + "/messages", headers=headers, json={"text": "hi"})).status in {404, 405}
            logs = await client.get(root + "/diagnostics")
            assert logs.status == 200 and "attachment" in logs.headers["Content-Disposition"]
            assert "credential_actions" not in await logs.json()
    finally:
        await gateway.close()
