"""The Web Chat Knowledge page's gateway side: routes, browser checks and refusals.

Reads need the signed-in session and the person's own Web Chat thread; changes
also need the CSRF header, the page's own origin and same-origin fetch
metadata. The browser never names a person; the gateway names editors and
never sends person ids. The manager's refusals keep their codes, plain words
and review flags.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.knowledge_pages import (
    TEAM_MEMBER_LABEL,
    YOU_LABEL,
    GatewayKnowledgePages,
)
from assistant_gateway.application.manager_commands import GatewayManagerCommandService
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeEditorPayload,
    KnowledgePageDetailPayload,
    KnowledgeReviewFlagPayload,
    ManagerKnowledgeCommand,
)
from tests.test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _config,
    _UserTokenStatefulFakeGateway,
)

SESSION_KEY = "website:web-chat:knowledge"
OTHER_SESSION_KEY = "website:web-chat:someone-else"


class _Runner:
    """Stands in for the manager: records each request and answers with ``reply``."""

    def __init__(self) -> None:
        self.calls: list[tuple[KnowledgeCommandPayload, object]] = []
        self.reply = ManagerKnowledgeCommand(
            command=KnowledgeCommandPayload(action=KnowledgeCommandAction.LIST),
            success=True,
            result=KnowledgeCommandResultPayload(action=KnowledgeCommandAction.LIST),
        )

    async def execute_knowledge_command(self, command, *, context):
        self.calls.append((command.command, context))
        return self.reply

    def refuse(self, code: str, message: str, **fields) -> None:
        self.reply = ManagerKnowledgeCommand(
            command=KnowledgeCommandPayload(action=KnowledgeCommandAction.WRITE),
            success=False, error_code=code, error_message=message, **fields,
        )


def _page(editor: KnowledgeEditorPayload) -> KnowledgePageDetailPayload:
    return KnowledgePageDetailPayload(
        space="team", page="deploys", title="Deploys", page_type="how-to", revision=2, status="active",
        updated_at_utc="2026-10-10T12:00:00.000000Z", body="Deploy on Tuesdays.",
        created_at_utc="2026-10-09T12:00:00.000000Z", last_editor=editor,
    )


@pytest.fixture
def site(tmp_path: Path):
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    for key, owner in ((SESSION_KEY, "user_alpha"), (OTHER_SESSION_KEY, "user_beta")):
        store.create_session(
            session_key=key, conversation_id="conv_" + owner, manager_agent_id="manager_1",
            channel_name="website", user_id=owner, chat_id="web-chat", owner_canonical_user_id=owner,
        )
    runner = _Runner()
    names = {"user_beta": "Beatrix", "user_gamma": "user_gamma"}
    gateway.knowledge = GatewayKnowledgePages(runner=runner, display_name=lambda pid: names.get(pid, pid), enabled=True)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    return SimpleNamespace(app=app, runner=runner, gateway=gateway)


async def _signed_in(client: TestClient) -> dict[str, str]:
    auth = await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})
    assert auth.status == 200
    return {
        "Origin": str(client.make_url("/")).rstrip("/"),
        "Sec-Fetch-Site": "same-origin",
        "X-UA-Permission-CSRF": (await auth.json())["permission_csrf_token"],
    }


@pytest.mark.asyncio
async def test_reads_need_the_session_and_the_persons_own_thread(site) -> None:
    async with TestClient(TestServer(site.app.build_app())) as client:
        signed_out = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}")
        signed_out_access = await client.get("/api/web-chat/knowledge/access")
        await _signed_in(client)
        access = await client.get("/api/web-chat/knowledge/access")
        access_payload = await access.json()
        listed = await client.get(
            f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}&space=personal&status=archived&query=deploy&offset=20"
        )
        someone_elses = await client.get(f"/api/web-chat/knowledge/pages?session_key={OTHER_SESSION_KEY}")
        no_thread = await client.get("/api/web-chat/knowledge/pages")

    assert (signed_out.status, signed_out_access.status) == (401, 401)
    assert access_payload["enabled"] is True
    assert access.headers["Cache-Control"] == "no-store"
    assert listed.status == 200
    assert (someone_elses.status, no_thread.status) == (404, 400)
    [(payload, context)] = site.runner.calls
    assert (payload.action, payload.space, payload.status, payload.query, payload.offset) == (
        KnowledgeCommandAction.LIST, "personal", "archived", "deploy", 20,
    )
    assert (context.canonical_user_id, context.session_key) == ("user_alpha", SESSION_KEY)


@pytest.mark.asyncio
async def test_changes_need_csrf_origin_and_same_origin_fetch(site) -> None:
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        body = {"session_key": SESSION_KEY, "title": "Deploys", "body": "Deploy on Tuesdays."}
        path = "/api/web-chat/knowledge/pages/team"
        no_csrf = await client.post(path, json=body, headers={**headers, "X-UA-Permission-CSRF": ""})
        other_origin = await client.post(path, json=body, headers={**headers, "Origin": "https://evil.example"})
        cross_site = await client.post(path, json=body, headers={**headers, "Sec-Fetch-Site": "cross-site"})
        assert site.runner.calls == []
        created = await client.post(
            path, headers=headers,
            json={**body, "base_revision": 7, "person_id": "user_beta", "canonical_user_id": "user_beta"},
        )
        noted = await client.post(
            "/api/web-chat/knowledge/pages/team/deploys/notes", headers=headers,
            json={"session_key": SESSION_KEY, "note": "Check the freeze calendar.", "confirm_review": True},
        )
        unknown = await client.post(
            "/api/web-chat/knowledge/pages/team/deploys/delete", headers=headers, json={"session_key": SESSION_KEY}
        )

    assert (no_csrf.status, other_origin.status, cross_site.status) == (403, 403, 403)
    assert (created.status, noted.status, unknown.status) == (200, 200, 404)
    (create, create_context), (note, _) = site.runner.calls
    # A new page is always a creation, and the body cannot name a person.
    assert (create.action, create.space, create.base_revision, create.title) == (
        KnowledgeCommandAction.WRITE, "team", 0, "Deploys",
    )
    assert create_context.canonical_user_id == "user_alpha"
    assert (note.action, note.page, note.note, note.confirm_review) == (
        KnowledgeCommandAction.ADD_NOTE, "deploys", "Check the freeze calendar.", True,
    )


@pytest.mark.asyncio
async def test_refusals_keep_their_code_words_and_review_flags(site) -> None:
    flag = KnowledgeReviewFlagPayload(kind="email_address", label="an email address", field="body", line=2, column=10)
    cases = [
        ("conflict", 409, {"current_revision": 3}),
        ("merged", 409, {"merged_into": "team/ci-pipeline"}),
        ("review_required", 409, {"review_flags": [flag]}),
        ("sensitive_content", 422, {}),
        ("not_found", 404, {}),
        ("authorization_denied", 403, {}),
        ("knowledge_off", 503, {}),
        ("invalid_request", 400, {}),
    ]
    answers = []
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        for code, _status, fields in cases:
            site.runner.refuse(code, f"Refused: {code}.", **fields)
            response = await client.post(
                "/api/web-chat/knowledge/pages/team/deploys/save", headers=headers,
                json={"session_key": SESSION_KEY, "base_revision": 2, "title": "Deploys", "body": "New text."},
            )
            answers.append((response.status, await response.json()))
        bad_tags = await client.post(
            "/api/web-chat/knowledge/pages/team/deploys/save", headers=headers,
            json={"session_key": SESSION_KEY, "base_revision": 2, "tags": "not-a-list"},
        )
        bad_tags_payload = await bad_tags.json()
        bad_status = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}&status=deleted")

    for (code, status, _fields), (got_status, payload) in zip(cases, answers):
        assert got_status == status, code
        assert payload["error"] == {"code": code, "message": f"Refused: {code}."}
    assert answers[0][1]["current_revision"] == 3
    assert answers[1][1]["merged_into"] == "team/ci-pipeline"
    assert answers[2][1]["review_flags"] == [
        {"kind": "email_address", "label": "an email address", "field": "body", "line": 2, "column": 10}
    ]
    assert bad_tags.status == 400
    assert bad_tags_payload["error"]["code"] == "invalid_request"
    assert bad_status.status == 400


@pytest.mark.asyncio
async def test_the_proposal_count_answers_only_the_number_for_the_persons_own_thread(site) -> None:
    site.runner.reply = ManagerKnowledgeCommand(
        command=KnowledgeCommandPayload(action=KnowledgeCommandAction.COUNT_CONTRIBUTIONS), success=True,
        result=KnowledgeCommandResultPayload(action=KnowledgeCommandAction.COUNT_CONTRIBUTIONS, total=3),
    )
    async with TestClient(TestServer(site.app.build_app())) as client:
        signed_out = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        await _signed_in(client)
        counted = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        counted_payload = await counted.json()
        someone_elses = await client.get(f"/api/web-chat/knowledge/counts?session_key={OTHER_SESSION_KEY}")
        site.runner.refuse("knowledge_off", "The knowledge base is turned off.")
        off = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        off_payload = await off.json()

    assert signed_out.status == 401
    assert counted.status == 200
    assert counted.headers["Cache-Control"] == "no-store"
    assert counted_payload == {"status": "ok", "pending_proposals": 3}
    assert someone_elses.status == 404
    assert (off.status, off_payload["error"]["code"]) == (503, "knowledge_off")
    [(payload, context), _off] = site.runner.calls
    assert payload.action is KnowledgeCommandAction.COUNT_CONTRIBUTIONS
    assert (context.canonical_user_id, context.session_key) == ("user_alpha", SESSION_KEY)


@pytest.mark.asyncio
async def test_editors_are_named_and_person_ids_never_reach_the_browser(site) -> None:
    async def page_by(editor):
        site.runner.reply = ManagerKnowledgeCommand(
            command=KnowledgeCommandPayload(action=KnowledgeCommandAction.READ), success=True,
            result=KnowledgeCommandResultPayload(action=KnowledgeCommandAction.READ, page=_page(editor)),
        )
        response = await client.get(f"/api/web-chat/knowledge/pages/team/deploys?session_key={SESSION_KEY}")
        return await response.text()

    async with TestClient(TestServer(site.app.build_app())) as client:
        await _signed_in(client)
        mine = await page_by(KnowledgeEditorPayload(kind="person", person_id="user_alpha"))
        named = await page_by(KnowledgeEditorPayload(kind="person", person_id="user_beta"))
        unnamed = await page_by(KnowledgeEditorPayload(kind="person", person_id="user_gamma"))
        by_agent = await page_by(KnowledgeEditorPayload(kind="agent", agent_type="KnowledgeCuratorAgent"))

    for text, label, person_id in (
        (mine, YOU_LABEL, "user_alpha"),
        (named, "Beatrix", "user_beta"),
        (unnamed, TEAM_MEMBER_LABEL, "user_gamma"),
        (by_agent, "KnowledgeCuratorAgent", ""),
    ):
        assert json.loads(text)["page"]["last_editor"]["label"] == label
        assert not person_id or person_id not in text


def test_the_gateway_checks_the_same_ownership_rule_before_sending() -> None:
    authorizer = OwnershipPrivateResourceAuthorizer()

    def allowed(payload: KnowledgeCommandPayload, roles: tuple[str, ...] = ("member",)) -> bool:
        target = GatewayManagerCommandService._private_resource_target(
            ManagerKnowledgeCommand(command=payload), session=SimpleNamespace(), person=SimpleNamespace(canonical_user_id="alice"),
        )
        return authorizer.can_access(
            actor_canonical_user_id="alice",
            owner_canonical_user_id="alice" if target.owned_by_person else None,
            resource_type=target.resource_type, resource_id=target.resource_id, action=target.action,
            team_id="team_1", roles=roles,
        )

    team_edit = KnowledgeCommandPayload(action=KnowledgeCommandAction.WRITE, page="deploys", base_revision=2)
    assert allowed(team_edit) is True
    assert allowed(team_edit, roles=()) is False
    assert allowed(KnowledgeCommandPayload(action=KnowledgeCommandAction.READ, space="personal", page="travel")) is True
    assert allowed(KnowledgeCommandPayload(action=KnowledgeCommandAction.PUBLISH_CONTRIBUTION, contribution_id="kb_contrib_1")) is True
    assert allowed(KnowledgeCommandPayload(action=KnowledgeCommandAction.COUNT_CONTRIBUTIONS)) is True
