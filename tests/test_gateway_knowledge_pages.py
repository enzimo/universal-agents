"""The Web Chat Knowledge page's gateway side: routes, browser checks and refusals.

Reads need the signed-in session and the person's own Web Chat thread; changes
also need the CSRF header, the page's own origin and same-origin fetch
metadata. The browser never names a person: the gateway sends the signed-in
person's reference, names editors, and never sends person ids or references to
the browser. The team services' refusals keep their codes, plain words and
review flags.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.knowledge_pages import (
    TEAM_MEMBER_LABEL,
    YOU_LABEL,
    GatewayKnowledgePages,
)
from assistant_gateway.application.knowledge_wire import INLINE_TEXT_MAX
from assistant_gateway.application.team_commands import (
    GatewayTeamCommandService,
    PersonCopyNotCurrent,
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)
from assistant_gateway.interactive_apps.website_handlers.knowledge import (
    CHANGE_UNKNOWN_TEXT,
    READ_FAILED_TEXT,
    REQUEST_REFUSED_TEXT,
    TEAM_SERVICES_SILENT_READ_TEXT,
    TEAM_SERVICES_SILENT_TEXT,
)
from assistant_gateway.store import GatewayStateStore
from tests.test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _config,
    _UserTokenStatefulFakeGateway,
)

SESSION_KEY = "website:web-chat:knowledge"
OTHER_SESSION_KEY = "website:web-chat:someone-else"
REFERENCES = {"user_alpha": "p-alpha", "user_beta": "p-beta", "user_gamma": "p-gamma"}


class _TeamServices:
    """Stands in for the team services: records each command and answers with ``reply``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.reply: dict = {"outcome": "ok", "pages": [], "total": 0}
        # Raised instead of answering: the team services refused the request itself or did not answer.
        self.error: Exception | None = None
        # Answered in turn before ``reply``, for lists that come in parts.
        self.replies: list[dict] = []

    async def command(self, kind: str, body: dict, *, person_ref: str, request_id: str) -> dict:
        assert body["request_id"] == request_id
        self.calls.append((kind, body, person_ref))
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if self.replies else self.reply

    def refuse(self, outcome: str, message: str, **fields) -> None:
        self.reply = {"outcome": outcome, "message": message, **fields}


class _References:
    def person_ref(self, person_id: str) -> str | None:
        return REFERENCES.get(person_id)

    def person_id(self, person_ref: str) -> str | None:
        return next((person for person, reference in REFERENCES.items() if reference == person_ref), None)


def _page(editor: dict) -> dict:
    return {
        "type": "inline", "space": "team", "page": "deploys", "title": "Deploys", "page_type": "how-to",
        "summary": "", "tags": [], "status": "active", "seen_version": "2", "updated_at": "2026-10-10T12:00:00.000Z",
        "content": {"type": "inline", "text": "Deploy on Tuesdays."}, "created_at": "2026-10-09T12:00:00.000Z",
        "updated_by": editor, "backlinks": [], "open_notes": [], "pending_contributions": [], "links": [],
    }


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
    team = _TeamServices()
    names = {"user_beta": "Beatrix", "user_gamma": "user_gamma"}
    references = _References()
    gateway.knowledge = GatewayKnowledgePages(
        team_commands=GatewayTeamCommandService(team_services=team, references=references, request_ids=lambda: "cmd-1"),
        references=references, display_name=lambda pid: names.get(pid, pid), enabled=True,
    )
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    return SimpleNamespace(app=app, team=team, gateway=gateway)


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
    [(kind, body, person_ref)] = site.team.calls
    assert kind == "knowledge.list" and person_ref == REFERENCES["user_alpha"]
    assert (body["space"], body["status"], body["query"], body["offset"]) == ("personal", "archived", "deploy", 20)


@pytest.mark.asyncio
async def test_changes_need_csrf_origin_and_same_origin_fetch(site) -> None:
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        body = {"session_key": SESSION_KEY, "title": "Deploys", "body": "Deploy on Tuesdays."}
        path = "/api/web-chat/knowledge/pages/team"
        no_csrf = await client.post(path, json=body, headers={**headers, "X-UA-Permission-CSRF": ""})
        other_origin = await client.post(path, json=body, headers={**headers, "Origin": "https://evil.example"})
        cross_site = await client.post(path, json=body, headers={**headers, "Sec-Fetch-Site": "cross-site"})
        assert site.team.calls == []
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
    (create_kind, create, create_person), (note_kind, note, _) = site.team.calls
    # A new page is always a creation (no version), and the body cannot name a person.
    assert (create_kind, create["space"], create["title"], "seen_version" in create) == (
        "knowledge.write", "team", "Deploys", False,
    )
    assert create_person == REFERENCES["user_alpha"] and "user_beta" not in json.dumps(create)
    assert (note_kind, note["page"], note["note"]["text"], note["confirm_review"]) == (
        "knowledge.add_note", "deploys", "Check the freeze calendar.", True,
    )


@pytest.mark.asyncio
async def test_refusals_keep_their_code_words_and_review_flags(site) -> None:
    flag = {"kind": "email_address", "label": "an email address", "field": "body", "line": 2, "column": 10}
    cases = [
        ("conflict", "changed_since_read", 409, {"current_version": "3"}),
        ("merged", "merged", 409, {"merged_into": "team/ci-pipeline"}),
        ("review_required", "review_required", 409, {"review_flags": [flag]}),
        ("sensitive_content", "sensitive_content", 422, {"review_flags": []}),
        ("not_found", "not_found", 404, {}),
        ("authorization_denied", "not_allowed", 403, {}),
        ("knowledge_off", "feature_off", 503, {}),
        ("invalid_request", "invalid_request", 400, {}),
    ]
    answers = []
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        for code, outcome, _status, fields in cases:
            site.team.refuse(outcome, f"Refused: {code}.", **fields)
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

    for (code, _outcome, status, _fields), (got_status, payload) in zip(cases, answers):
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
@pytest.mark.parametrize(
    ("error", "change", "message"),
    [
        (TeamCommandRefused("knowledge.add_note", "handler_failed"), True, CHANGE_UNKNOWN_TEXT),
        (TeamCommandRefused("knowledge.list", "handler_failed"), False, READ_FAILED_TEXT),
        (TeamCommandRefused("knowledge.add_note", "revocations_not_current"), True, REQUEST_REFUSED_TEXT),
        (TeamServicesNoAnswer("The team services did not answer in time."), True, TEAM_SERVICES_SILENT_TEXT),
        (TeamServicesNoAnswer("The team services did not answer in time."), False, TEAM_SERVICES_SILENT_READ_TEXT),
        (PersonCopyNotCurrent(), True, str(PersonCopyNotCurrent())),
        (TeamServicesUnavailable("You are not a member of this team, so the command was not sent."), True,
         "You are not a member of this team, so the command was not sent."),
    ],
    ids=["change-handler-failed", "read-handler-failed", "change-refused", "change-unanswered", "read-unanswered",
         "copy-not-current", "not-a-member"],
)
async def test_a_refused_or_unanswered_request_is_explained_in_plain_words(site, caplog, error, change, message) -> None:
    """``handler_failed`` leaves a change's result unknown; other refusals came before anything ran."""

    site.team.error = error
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        with caplog.at_level(logging.WARNING):
            if change:
                response = await client.post("/api/web-chat/knowledge/pages/team/deploys/notes", headers=headers,
                                             json={"session_key": SESSION_KEY, "note": "Check the calendar."})
            else:
                response = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}")
        payload = await response.json()

    assert response.status == 503
    assert payload["error"] == {"code": "runtime_error", "message": message}
    # The internal code reaches the log, not the person.
    code = getattr(error, "code", None)
    if code is not None:
        assert code not in payload["error"]["message"] and f"code={code}" in caplog.text


@pytest.mark.asyncio
async def test_an_offset_past_the_contracts_limit_is_refused_before_sending(site) -> None:
    async with TestClient(TestServer(site.app.build_app())) as client:
        await _signed_in(client)
        last = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}&offset=100000")
        past = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}&offset=100001")
        past_payload = await past.json()

    assert (last.status, past.status, past_payload["error"]["code"]) == (200, 400, "invalid_request")
    assert [body["offset"] for _kind, body, _person in site.team.calls] == [100000]


@pytest.mark.asyncio
async def test_a_list_that_comes_in_parts_reaches_the_browser_whole(site) -> None:
    def summary(number: int) -> dict:
        return {"space": "team", "page": f"page-{number:02d}", "title": f"Page {number}", "page_type": "reference",
                "summary": "", "tags": [], "status": "active", "seen_version": "1",
                "updated_at": "2026-10-10T12:00:00.000Z"}

    site.team.replies = [
        {"outcome": "ok", "pages": [summary(n) for n in range(0, 8)], "total": 25, "next_offset": 8},
        {"outcome": "ok", "pages": [summary(n) for n in range(8, 20)], "total": 25, "next_offset": 20},
    ]
    async with TestClient(TestServer(site.app.build_app())) as client:
        await _signed_in(client)
        response = await client.get(f"/api/web-chat/knowledge/pages?session_key={SESSION_KEY}")
        payload = await response.json()

    assert [page["page"] for page in payload["pages"]] == [f"page-{n:02d}" for n in range(20)]
    assert payload["total"] == 25
    assert [(body["offset"], body["limit"]) for _kind, body, _person in site.team.calls] == [(0, 20), (8, 12)]


@pytest.mark.asyncio
async def test_the_proposal_count_answers_only_the_number_for_the_persons_own_thread(site) -> None:
    site.team.reply = {"outcome": "ok", "total": 3}
    async with TestClient(TestServer(site.app.build_app())) as client:
        signed_out = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        await _signed_in(client)
        counted = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        counted_payload = await counted.json()
        someone_elses = await client.get(f"/api/web-chat/knowledge/counts?session_key={OTHER_SESSION_KEY}")
        site.team.refuse("feature_off", "The knowledge base is turned off.")
        off = await client.get(f"/api/web-chat/knowledge/counts?session_key={SESSION_KEY}")
        off_payload = await off.json()

    assert signed_out.status == 401
    assert counted.status == 200
    assert counted.headers["Cache-Control"] == "no-store"
    assert counted_payload == {"status": "ok", "pending_proposals": 3}
    assert someone_elses.status == 404
    assert (off.status, off_payload["error"]["code"]) == (503, "knowledge_off")
    [(kind, body, person_ref), _off] = site.team.calls
    assert (kind, person_ref) == ("knowledge.count_contributions", REFERENCES["user_alpha"])


@pytest.mark.asyncio
async def test_editors_are_named_and_person_ids_never_reach_the_browser(site) -> None:
    async def page_by(editor):
        site.team.reply = {"outcome": "ok", "page": _page(editor)}
        response = await client.get(f"/api/web-chat/knowledge/pages/team/deploys?session_key={SESSION_KEY}")
        return await response.text()

    async with TestClient(TestServer(site.app.build_app())) as client:
        await _signed_in(client)
        mine = await page_by({"kind": "person", "person": REFERENCES["user_alpha"]})
        named = await page_by({"kind": "person", "person": REFERENCES["user_beta"]})
        unnamed = await page_by({"kind": "person", "person": REFERENCES["user_gamma"]})
        stranger = await page_by({"kind": "person", "person": "p-unknown"})
        by_agent = await page_by({"kind": "agent", "agent_type": "KnowledgeCuratorAgent"})

    for text, label, person_id in (
        (mine, YOU_LABEL, "user_alpha"),
        (named, "Beatrix", "user_beta"),
        (unnamed, TEAM_MEMBER_LABEL, "user_gamma"),
        (stranger, TEAM_MEMBER_LABEL, "p-unknown"),
        (by_agent, "KnowledgeCuratorAgent", ""),
    ):
        assert json.loads(text)["page"]["last_editor"]["label"] == label
        assert not person_id or person_id not in text
        assert not person_id or REFERENCES.get(person_id, person_id) not in text


@pytest.mark.asyncio
async def test_text_longer_than_web_chat_carries_is_refused_plainly(site) -> None:
    """Longer typed text is refused before anything is sent; the team services' refusal of longer stored text reaches the browser as written."""

    exact, over = "x" * INLINE_TEXT_MAX, "x" * (INLINE_TEXT_MAX + 1)
    too_long = (
        "This page's text has 40,000 characters, more than the 32,768 Web Chat can show. Nothing was changed and "
        "nothing was cut. Agents can still read and change all of it, for example to split the page into smaller "
        "linked pages."
    )
    async with TestClient(TestServer(site.app.build_app())) as client:
        headers = await _signed_in(client)
        save = "/api/web-chat/knowledge/pages/team/deploys/save"
        refused_save = await client.post(save, headers=headers, json={"session_key": SESSION_KEY, "base_revision": 2,
                                                                      "body": over})
        refused_note = await client.post("/api/web-chat/knowledge/pages/team/deploys/notes", headers=headers,
                                         json={"session_key": SESSION_KEY, "note": over})
        typed = [(response.status, await response.json()) for response in (refused_save, refused_note)]
        assert site.team.calls == []
        sent = await client.post(save, headers=headers, json={"session_key": SESSION_KEY, "base_revision": 2,
                                                              "body": exact})
        site.team.refuse("runtime_error", too_long)
        refused_read = await client.get(f"/api/web-chat/knowledge/pages/team/handbook?session_key={SESSION_KEY}")
        refused_payload = await refused_read.json()

    for status, payload in typed:
        assert status == 400
        assert payload["error"] == {
            "code": "invalid_request",
            "message": f"The text is {INLINE_TEXT_MAX + 1} characters long; at most {INLINE_TEXT_MAX} can be sent. Shorten it.",
        }
    assert sent.status == 200
    [(kind, body, _person), _read] = site.team.calls
    assert kind == "knowledge.write" and body["content"]["text"] == exact
    assert refused_read.status == 503
    assert refused_payload["error"] == {"code": "runtime_error", "message": too_long}


def test_every_knowledge_action_becomes_a_contract_body() -> None:
    from assistant_gateway.application import knowledge_wire
    from custom_messages.contracts.control_v1 import KnowledgeCommandAction

    assert set(knowledge_wire._BODIES) == set(KnowledgeCommandAction)
