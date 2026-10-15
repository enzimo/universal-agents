"""The team's model keys in the broker, end to end over HTTP.

The broker is the real service with the real file-backed credential store; a
small fake OpenAI-compatible server accepts exactly one key. Covered:

* only administrators open the ``/auth model`` page and submit it;
* a key is tested with a real request before it is stored: a failing key
  stores nothing, the page says why without echoing the key, and the same
  page then saves a working key;
* "no key needed" and removing a key;
* the key goes only to processes that run agents (manager, agent
  processes), never to the gateway; the gateway reads a status that never
  holds a key but names environment overrides and rejections;
* saving a new key clears the old rejection;
* an address typed without ``/v1`` (or with a path that answers HTTP 405 or a
  web page) is tried in its other form, and the page says which one works;
* the page has one key form: its Endpoint list (the team's endpoints,
  well-known providers, a new address) fills in the fields;
* a failure shows in red at the top of the form, with what it means, and the
  form keeps what was typed (never the key);
* the page lasts 24 hours, its Close button ends it, and each submission
  re-checks the person's current standing and roles.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from access_control.caller_tokens import CALLER_MANAGER, agent_caller_label
from secure_tool_broker.defaults import MODEL_KEY_LOOKUP_ROUTE, MODEL_KEY_REPORT_ROUTE, MODEL_KEY_STATUS_ROUTE
from custom_messages.contracts.model_keys_v1 import (
    ConnectionPurposeV1,
    ModelKeyEndpointUseV1,
)
from model_providers.endpoints import EndpointKind
from secure_tool_broker.auth_flows import BrokerAuthSession
from secure_tool_broker.model_key_page import ModelKeyFlow, model_key_reports, team_model_key_store
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, caller_headers, gateway_headers, open_gate

GOOD_KEY = "sk-good-model-key-canary-0001"
BAD_KEY = "sk-wrong-model-key-canary-0002"
MODEL = "openai/gpt-6-luna"


class FakeOpenAICompatible:
    """Answers chat completions and embeddings for one key; refuses every other key with 401."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_post("/v1/embeddings", self._embeddings)
        # Like bifrost.example.net without /v1: the path exists for GET only, so a POST answers 405.
        app.router.add_get("/chat/completions", self._web_page)
        # A site that answers every path with a web page.
        app.router.add_post("/site/chat/completions", self._web_page)
        app.router.add_post("/site/v1/chat/completions", self._web_page)
        return app

    async def _web_page(self, _request: web.Request) -> web.Response:
        return web.Response(text="<html><body>Welcome</body></html>", content_type="text/html")

    def _refusal(self, request: web.Request) -> web.Response | None:
        if request.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            return web.json_response({"error": {"message": "Incorrect API key provided"}}, status=401)
        return None

    async def _chat(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return web.json_response({"choices": [{"message": {"content": "I'm connected and ready to go!"}}]})

    async def _embeddings(self, request: web.Request) -> web.Response:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return web.json_response({"data": [{"embedding": [0.1, 0.2]}]})


@pytest.fixture(autouse=True)
def file_backed_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    # One store and report record per broker process; each test is a fresh broker.
    team_model_key_store.cache_clear()
    model_key_reports.cache_clear()


@pytest_asyncio.fixture
async def provider() -> tuple[FakeOpenAICompatible, str]:
    fake = FakeOpenAICompatible()
    server = TestServer(fake.build_app())
    await server.start_server()
    yield fake, str(server.make_url("/v1")).rstrip("/")
    await server.close()


@pytest_asyncio.fixture
async def broker() -> TestClient:
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate()).build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


def _context(person: str, roles: list[str]) -> dict:
    return {"canonical_user_id": person, "principal_type": "user", "team_id": TEST_TEAM, "person_roles": roles}


async def _open_page(client: TestClient, roles: list[str], endpoints: list[ModelKeyEndpointUseV1] = ()) -> web.ClientResponse:
    return await client.post(
        "/v1/auth/model/sessions",
        headers=gateway_headers(),
        json={
            "context": _context("admin-1", roles),
            "method": "team",
            "endpoints": [json.dumps(use.model_dump(mode="json")) for use in endpoints],
        },
    )


async def _page_path(client: TestClient, endpoints: list[ModelKeyEndpointUseV1] = ()) -> str:
    created = await _open_page(client, ["admin"], endpoints)
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["login_url"].split("/", 3)[3]


async def _lookup(client: TestClient, base_url: str, label: str = CALLER_MANAGER) -> web.ClientResponse:
    return await client.post(MODEL_KEY_LOOKUP_ROUTE, headers=caller_headers(label), json={"address": base_url})


@pytest.mark.asyncio
@pytest.mark.parametrize(("roles", "allowed"), [(["member"], False), (["admin"], True), (["owner"], True)])
async def test_only_administrators_open_the_model_key_page(broker: TestClient, roles: list[str], allowed: bool) -> None:
    created = await _open_page(broker, roles)

    if allowed:
        assert created.status == 200
        return
    assert created.status == 403
    body = json.loads(await created.text())
    assert body["reason_code"] == "model_team_admin_required"
    assert body["error"] == "Only a team administrator or owner can change the team's model provider keys."


def test_a_submission_without_administrator_roles_stores_nothing() -> None:
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="model", method="team", created_at=now, expires_at=now + timedelta(minutes=5),
        owner_canonical_user_id="member-1", owner_team_id=TEST_TEAM, owner_roles=("member",),
    )

    ModelKeyFlow().submit(session, {"base_url": "https://bifrost.example.com/v1", "model": MODEL, "key": GOOD_KEY}, _logger())

    assert "administrator" in session.state["error"]
    assert team_model_key_store(TEST_TEAM).entries() == ()


@pytest.mark.asyncio
async def test_a_failing_key_is_not_saved_and_the_same_page_then_saves_a_working_one(broker, provider) -> None:
    fake, base_url = provider
    use = ModelKeyEndpointUseV1(base_url=base_url, kind=EndpointKind.OPENAI_COMPATIBLE, model_id=MODEL, label="Team model endpoint")
    page = await _page_path(broker, [use])
    form = await (await broker.get(page)).text()
    assert "Team model endpoint (openai/gpt-6-luna)" in form
    assert "Agents cannot call this endpoint until a key is saved here." in form

    refused = await (
        await broker.post(page, data={"form_id": "key-form", "action": "save", "base_url": base_url, "model": MODEL, "key": BAD_KEY})
    ).text()

    assert "Nothing was saved" in refused and "The provider refused the key." in refused
    # Red, and at the top of the form that was used (which the page scrolls to).
    assert '<h2>Save a Key</h2><p class="status status-error" role="alert">Nothing was saved.' in refused
    assert '<form method="post" action="#key-form">' in refused
    assert BAD_KEY not in refused
    assert team_model_key_store(TEST_TEAM).entries() == ()
    assert (await (await _lookup(broker, base_url)).json())["state"] == "missing"

    saved = await (await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})).text()

    assert "The key works" in saved and "no restart is needed" in saved
    assert GOOD_KEY not in saved
    reply = await (await _lookup(broker, base_url)).json()
    assert reply["state"] == "stored" and reply["key"] == GOOD_KEY
    assert fake.requests[-1]["model"] == MODEL and fake.requests[-1]["tools"]


@pytest.mark.asyncio
async def test_an_embedding_key_is_tested_with_an_embedding(broker, provider) -> None:
    _fake, base_url = provider
    page = await _page_path(broker)

    saved = await (
        await broker.post(
            page,
            data={"action": "save", "base_url": base_url, "purpose": ConnectionPurposeV1.EMBEDDING.value, "model": "embed-1", "key": GOOD_KEY},
        )
    ).text()

    assert "The key works" in saved


@pytest.mark.asyncio
async def test_no_key_needed_still_tests_the_endpoint_and_a_key_can_be_removed(broker, provider) -> None:
    _fake, base_url = provider
    page = await _page_path(broker)

    refused = await (await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "no_key": "1"})).text()
    assert "Nothing was saved" in refused

    await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})
    removed = await (await broker.post(page, data={"action": "remove", "base_url": base_url})).text()

    assert "is removed" in removed
    assert (await (await _lookup(broker, base_url)).json())["state"] == "missing"


@pytest.mark.asyncio
async def test_addresses_with_credentials_or_queries_are_refused(broker) -> None:
    page = await _page_path(broker)

    for address in ("https://user:pw@bifrost.example.com/v1", "https://bifrost.example.com/v1?key=x", "ftp://bifrost"):
        text = await (await broker.post(page, data={"action": "save", "base_url": address, "model": MODEL, "key": GOOD_KEY})).text()
        assert "endpoint address" in text
    assert team_model_key_store(TEST_TEAM).entries() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "allowed"),
    [(CALLER_MANAGER, True), (agent_caller_label("GeneralAssistant"), True), ("gateway", False)],
)
async def test_keys_go_only_to_processes_that_make_model_calls(broker, provider, label: str, allowed: bool) -> None:
    _fake, base_url = provider
    page = await _page_path(broker)
    await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})

    reply = await _lookup(broker, base_url, label)

    assert reply.status == (200 if allowed else 403)
    if allowed:
        assert (await reply.json())["key"] == GOOD_KEY


@pytest.mark.asyncio
async def test_the_gateway_status_names_overrides_and_rejections_but_never_a_key(broker, provider) -> None:
    _fake, base_url = provider
    page = await _page_path(broker)
    await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})
    report = {"address": base_url, "kind": "openai_compatible", "event": "in_use", "source": "environment", "env_var": "LITELLM_API_KEY"}
    assert (await broker.post(MODEL_KEY_REPORT_ROUTE, headers=caller_headers(), json=report)).status == 200
    rejected = {**report, "event": "rejected", "source": "stored", "env_var": None, "model_id": MODEL, "status_code": 401}
    agent = agent_caller_label("GeneralAssistant")
    await broker.post(MODEL_KEY_REPORT_ROUTE, headers=caller_headers(agent), json=rejected)

    status = await broker.get(MODEL_KEY_STATUS_ROUTE, headers=gateway_headers())
    text = await status.text()

    assert status.status == 200 and GOOD_KEY not in text
    endpoint = json.loads(text)["endpoints"][0]
    assert endpoint["state"] == "stored" and endpoint["tested_model_id"] == MODEL
    assert endpoint["last_rejection"]["status_code"] == 401
    assert (await broker.get(MODEL_KEY_STATUS_ROUTE, headers=caller_headers())).status == 403
    page_text = await (await broker.get(page)).text()
    assert "The manager process gets LITELLM_API_KEY from team.env or the container&#x27;s environment" in page_text

    await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})

    cleared = json.loads(await (await broker.get(MODEL_KEY_STATUS_ROUTE, headers=gateway_headers())).text())
    assert cleared["endpoints"][0]["last_rejection"] is None


def _logger():
    import logging

    return logging.getLogger("test_broker_model_keys")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_runtime_fetches_a_saved_key_from_the_real_broker_and_reports_its_source(broker, provider, monkeypatch) -> None:
    import asyncio

    from access_control.caller_tokens import derive_caller_token
    from custom_agents.model_keys import HttpModelKeyBroker, ModelKeys
    from model_providers.endpoints import openai_compatible_endpoint

    _fake, base_url = provider
    page = await _page_path(broker)
    await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(broker.make_url("")).rstrip("/"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", derive_caller_token(BROKER_ROOT, CALLER_MANAGER))
    keys = ModelKeys(HttpModelKeyBroker(), env_lookup={}.get)

    resolved = await asyncio.to_thread(keys.resolve, openai_compatible_endpoint(base_url))

    assert resolved.value == GOOD_KEY
    status = json.loads(await (await broker.get(MODEL_KEY_STATUS_ROUTE, headers=gateway_headers())).text())
    assert status["endpoints"][0]["sources"][0]["caller"] == CALLER_MANAGER
    assert status["endpoints"][0]["sources"][0]["source"] == "stored"


@pytest.mark.asyncio
async def test_a_well_known_provider_key_is_saved_for_its_fixed_address(broker, monkeypatch) -> None:
    from model_providers import connection_test
    from model_providers.connection_test import ConnectionTestResult

    tested = []
    monkeypatch.setattr(
        "model_providers.connection_test.run_connection_test",
        lambda request: tested.append(request) or ConnectionTestResult(ok=True, message="ok"),
    )
    _ = connection_test
    page = await _page_path(broker)

    saved = await (
        await broker.post(page, data={"action": "save", "kind": "xai", "base_url": "https://ignored.example.com", "model": "grok-4.5", "key": GOOD_KEY})
    ).text()

    assert "api.x.ai" in saved
    assert tested[0].base_url == "https://api.x.ai/v1"
    assert team_model_key_store(TEST_TEAM).get("https://api.x.ai").kind is EndpointKind.XAI


# A person's own keys (/auth model me) ------------------------------------------------------


async def _personal_page(client: TestClient, person: str, endpoints: list[ModelKeyEndpointUseV1]) -> str:
    created = await client.post(
        "/v1/auth/model/sessions",
        headers=gateway_headers(),
        json={
            "context": _context(person, ["member"]),
            "method": "personal",
            "endpoints": [json.dumps(use.model_dump(mode="json")) for use in endpoints],
        },
    )
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["login_url"].split("/", 3)[3]


@pytest.mark.asyncio
async def test_a_member_saves_their_own_key_only_for_endpoints_the_team_uses(broker, provider) -> None:
    from secure_tool_broker.model_key_page import person_model_key_store

    person_model_key_store.cache_clear()
    _fake, base_url = provider
    use = ModelKeyEndpointUseV1(base_url=base_url, kind=EndpointKind.OPENAI_COMPATIBLE, model_id=MODEL)
    page = await _personal_page(broker, "alice", [use])
    assert "Your model and keys" in await (await broker.get(page)).text()

    elsewhere = await (
        await broker.post(page, data={"action": "save", "base_url": "https://other.example.com/v1", "model": MODEL, "key": GOOD_KEY})
    ).text()
    saved = await (await broker.post(page, data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})).text()

    assert "only for an endpoint the team uses" in elsewhere
    assert "saved as yours" in saved
    assert person_model_key_store("alice").get(base_url) is not None
    assert team_model_key_store(TEST_TEAM).entries() == ()


@pytest.mark.asyncio
async def test_a_persons_key_goes_only_to_tasks_whose_signed_context_names_them(broker, provider) -> None:
    from secure_tool_broker.model_key_page import person_model_key_store
    from tests.broker_access_support import agent_context

    person_model_key_store.cache_clear()
    _fake, base_url = provider
    use = ModelKeyEndpointUseV1(base_url=base_url, kind=EndpointKind.OPENAI_COMPATIBLE, model_id=MODEL)
    await broker.post(await _personal_page(broker, "alice", [use]), data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})

    def lookup(context):
        return broker.post(MODEL_KEY_LOOKUP_ROUTE, headers=caller_headers(), json={"address": base_url, "context": context})

    alices = await (await lookup(agent_context("alice"))).json()
    bobs = await (await lookup(agent_context("bob"))).json()
    forged = await lookup({**agent_context("bob"), "canonical_user_id": "alice"})

    assert (alices["owner"], alices["key"]) == ("person", GOOD_KEY)
    assert (bobs["owner"], bobs["state"]) == ("team", "missing")
    assert forged.status == 403


@pytest.mark.asyncio
async def test_the_gateway_may_look_up_only_the_xai_key_for_voice(broker, monkeypatch) -> None:
    from model_providers.connection_test import ConnectionTestResult

    monkeypatch.setattr(
        "model_providers.connection_test.run_connection_test", lambda request: ConnectionTestResult(ok=True, message="ok")
    )
    page = await _page_path(broker)
    await broker.post(page, data={"action": "save", "kind": "xai", "model": "grok-4.5", "key": GOOD_KEY})
    await broker.post(page, data={"action": "save", "kind": "openai", "model": "gpt-5", "key": GOOD_KEY})

    voice = await _lookup(broker, "https://api.x.ai", "gateway")
    other = await _lookup(broker, "https://api.openai.com", "gateway")
    with_context = await broker.post(
        MODEL_KEY_LOOKUP_ROUTE, headers=gateway_headers(), json={"address": "https://api.x.ai", "context": {"canonical_user_id": "a"}}
    )

    assert voice.status == 200 and (await voice.json())["key"] == GOOD_KEY
    assert other.status == 403 and with_context.status == 403


# Addresses with and without /v1, where results show, closing, live checks --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/"])
async def test_an_address_without_v1_is_tried_with_it_and_the_page_says_which_works(broker, provider, path) -> None:
    fake, base_url = provider
    host = base_url.removesuffix("/v1")
    page = await _page_path(broker)

    saved = await (
        await broker.post(
            page,
            data={"form_id": "key-form", "preset": "new", "action": "save", "base_url": host + path, "model": MODEL, "key": GOOD_KEY},
        )
    ).text()

    assert "The key works" in saved
    assert f"The endpoint answers at {host}/v1, not at {host};" in saved
    assert '<h2>Save a Key</h2><p class="status status-success" role="status">' in saved
    assert team_model_key_store(TEST_TEAM).get(host) is not None
    assert fake.requests[-1]["model"] == MODEL


@pytest.mark.asyncio
async def test_an_address_where_nothing_answers_explains_both_tries_and_keeps_what_was_typed(broker, provider) -> None:
    _fake, base_url = provider
    site = base_url.removesuffix("/v1") + "/site"
    page = await _page_path(broker)

    refused = await (
        await broker.post(
            page,
            data={"form_id": "key-form", "preset": "new", "action": "save", "base_url": site, "model": MODEL, "key": GOOD_KEY},
        )
    ).text()

    assert f"Nothing at {site} and {site}/v1 accepts model requests." in refused
    assert "not a model response" in refused
    assert '<h2>Save a Key</h2><p class="status status-error" role="alert">' in refused
    assert f'value="{site}"' in refused and f'value="{MODEL}"' in refused
    assert '<option value="new" data-kind="openai_compatible"' in refused and 'selected>New OpenAI-compatible address' in refused
    assert GOOD_KEY not in refused
    assert team_model_key_store(TEST_TEAM).entries() == ()


@pytest.mark.asyncio
async def test_the_page_lasts_a_day_and_close_ends_it(broker) -> None:
    created = await _open_page(broker, ["admin"])
    body = await created.json()
    lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=59) < lifetime <= timedelta(hours=24)
    page = "/" + body["login_url"].split("/", 3)[3]
    opened = await (await broker.get(page)).text()
    assert opened.count(">Close</button>") == 2
    assert "This page stays open for 24 hours." in opened

    closed = await (await broker.post(page, data={"form_id": "page-top", "action": "close"})).text()

    assert "This page is closed." in closed and "window.close()" in closed
    assert (await broker.get(page)).status == 410
    assert (await broker.post(page, data={"action": "save"})).status == 410


class _People:
    """The gateway's copy of the team's people, as the broker reads it."""

    def __init__(self, person) -> None:
        self._person = person

    def synced(self) -> bool:
        return True

    def person(self, person_id: str):
        return self._person


@pytest.mark.parametrize(
    ("standing", "roles", "message"),
    [
        ("active", ("member",), "Only a team administrator or owner"),
        ("disabled", ("admin",), "not active in this team"),
    ],
)
def test_each_submission_rechecks_the_persons_current_standing_and_roles(standing, roles, message) -> None:
    from access_control.people_directory import PersonNow
    from custom_messages.contracts.access_v2 import PersonStanding
    from secure_tool_broker.model_key_page import use_people_directory

    now = datetime.now(timezone.utc)
    # Signed in as an administrator when the page opened; the record says otherwise now.
    session = BrokerAuthSession(
        provider="model", method="team", created_at=now, expires_at=now + timedelta(hours=20),
        owner_canonical_user_id="admin-1", owner_team_id=TEST_TEAM, owner_roles=("admin",),
    )
    use_people_directory(_People(PersonNow("admin-1", PersonStanding(standing), roles)))
    try:
        ModelKeyFlow().submit(session, {"base_url": "https://bifrost.example.com/v1", "model": MODEL, "key": GOOD_KEY}, _logger())
    finally:
        use_people_directory(None)

    assert message in session.state["error"]
    assert team_model_key_store(TEST_TEAM).entries() == ()


@pytest.mark.asyncio
async def test_the_page_has_one_key_form_whose_endpoint_list_fills_the_fields(broker, provider) -> None:
    _fake, base_url = provider
    use = ModelKeyEndpointUseV1(base_url=base_url, kind=EndpointKind.OPENAI_COMPATIBLE, model_id=MODEL, label="Team model endpoint")
    page = await _page_path(broker, [use])

    text = await (await broker.get(page)).text()

    assert text.count("<fieldset>") == 1 and text.count(">Test and Save</button>") == 1
    # The team's endpoint is chosen and filled in; well-known providers and a new address are offered.
    assert f'<option value="use0" data-kind="openai_compatible" data-base-url="{base_url}"' in text
    assert f'value="{base_url}"' in text and f'value="{MODEL}"' in text
    assert 'data-base-url="https://api.x.ai/v1"' in text and 'data-fixed-address="1"' in text
    assert ">New OpenAI-compatible address</option>" in text
    assert "data-model-key-preset" in text


@pytest_asyncio.fixture
async def stored_broker(tmp_path):
    """A broker with an access store, as in a real instance (people's settings and own models)."""

    from tests.broker_access_support import access_store, linked_gate

    store = access_store(tmp_path / "access.db")
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=linked_gate(store)).build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, store
    await client.close()


@pytest.mark.asyncio
async def test_a_person_saves_their_own_model_at_any_address_with_the_form_that_answers(stored_broker, provider) -> None:
    from access_control.person_settings import PersonSettings
    from secure_tool_broker.model_key_page import person_model_key_store

    person_model_key_store.cache_clear()
    broker, store = stored_broker
    _fake, base_url = provider
    host = base_url.removesuffix("/v1")
    page = await _personal_page(broker, "alice", [])
    form = await (await broker.get(page)).text()
    assert form.count("<fieldset>") == 1 and ">New OpenAI-compatible address</option>" in form
    assert 'name="use_as_model"' in form and "You have no model of your own." in form

    refused = await (
        await broker.post(page, data={"form_id": "key-form", "preset": "new", "action": "save", "base_url": host, "model": MODEL, "key": GOOD_KEY})
    ).text()
    saved = await (
        await broker.post(
            page,
            data={
                "form_id": "key-form", "preset": "new", "action": "save", "base_url": host, "model": MODEL,
                "key": GOOD_KEY, "use_as_model": "1", "supports_images": "1", "context_window": "200,000",
            },
        )
    ).text()

    assert "Without “Use this as my model”" in refused
    assert f"Your model is saved: {MODEL} at 127.0.0.1" in saved and f"It answers at {base_url}." in saved
    assert f"All agents' work for you uses <strong>{MODEL}</strong>" in saved
    profile = PersonSettings(store, TEST_TEAM).own_model("alice")
    assert profile is not None and profile.base_url == base_url and profile.supports_images is True
    assert profile.context_window_tokens == 200_000 and profile.supports_files is False
    assert person_model_key_store("alice").get(host) is not None
    assert team_model_key_store(TEST_TEAM).entries() == ()

    removed = await (await broker.post(page, data={"form_id": "your-model", "action": "remove_model"})).text()

    assert "Your model is removed. Agents use the team&#x27;s models for your work again" in removed
    assert PersonSettings(store, TEST_TEAM).own_model("alice") is None


@pytest.mark.asyncio
async def test_a_person_without_the_teams_keys_is_told_in_red_and_bad_windows_are_refused(stored_broker, provider) -> None:
    from access_control.person_settings import PersonSettings

    broker, store = stored_broker
    _fake, base_url = provider
    PersonSettings(store, TEST_TEAM).set_team_keys("bob", False, by="admin-1")
    page = await _personal_page(broker, "bob", [])

    text = await (await broker.get(page)).text()
    refused = await (
        await broker.post(
            page,
            data={"form_id": "key-form", "action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY,
                  "use_as_model": "1", "context_window": "lots"},
        )
    ).text()

    assert """<p class="status status-error" role="alert">You haven't set up your own model yet.""" in text
    # "Use this as my model" is ticked for them from the start.
    assert 'name="use_as_model" value="1" checked' in text
    assert "The context window is a number of tokens, at least 1024" in refused
    assert PersonSettings(store, TEST_TEAM).own_model("bob") is None


# Whether a person's work may use the team's keys (access_control.person_settings) --------------


@pytest.mark.asyncio
async def test_the_broker_refuses_the_teams_key_for_a_person_whose_work_may_not_use_it(provider, tmp_path) -> None:
    from access_control.person_settings import PersonSettings
    from secure_tool_broker.model_key_page import person_model_key_store
    from tests.broker_access_support import access_store, agent_context, linked_gate

    person_model_key_store.cache_clear()
    _fake, base_url = provider
    store = access_store(tmp_path / "access.db")
    people = PersonSettings(store, TEST_TEAM)
    people.set_team_keys("alice", False, by="admin-1")
    people.set_team_keys("carol", True, by="admin-1")
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=linked_gate(store)).build_app()
    async with TestClient(TestServer(app)) as broker:
        await broker.post(await _page_path(broker), data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})

        def lookup(person: str, purpose: str = "chat"):
            body = {"address": base_url, "context": agent_context(person), "purpose": purpose}
            return broker.post(MODEL_KEY_LOOKUP_ROUTE, headers=caller_headers(), json=body)

        alice = await (await lookup("alice")).json()
        alice_embedding = await (await lookup("alice", "embedding")).json()
        carol = await (await lookup("carol")).json()
        # Added before the switch existed: no record, so the team's keys as before.
        dave = await (await lookup("dave")).json()
        use = ModelKeyEndpointUseV1(base_url=base_url, kind=EndpointKind.OPENAI_COMPATIBLE, model_id=MODEL)
        await broker.post(await _personal_page(broker, "alice", [use]), data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})
        alice_own = await (await lookup("alice")).json()

    assert (alice["team_keys_allowed"], alice["key"]) == (False, "")
    assert alice_embedding["key"] == GOOD_KEY and alice_embedding["team_keys_allowed"] is True
    assert carol["key"] == GOOD_KEY and dave["key"] == GOOD_KEY
    assert (alice_own["owner"], alice_own["key"]) == ("person", GOOD_KEY)


class _GatewayEvents:
    """The gateway's user events, as far as adding a person writes them."""

    def record_gateway_user_event(self, **_event) -> str:
        return "event-1"

    def gateway_user_event_types(self, **_query) -> set[str]:
        return set()


@pytest.mark.asyncio
async def test_people_added_while_the_team_keeps_its_model_keys_private_get_no_team_key(provider, tmp_path) -> None:
    # The whole chain: the team's model-key sharing when someone is added
    # (access_control.account_sharing), the gateway's add step that stores it
    # as their switch, and the broker's lookup that follows the switch.
    from access_catalog.catalog import CatalogSource
    from access_control.account_sharing import TeamAccountSharing
    from assistant_gateway.application.person_setup import PersonSetupChoices
    from assistant_gateway.person_setup import GatewayPersonSetupStore
    from assistant_gateway.personal_setup import GatewayPersonalSetup, PersonalSetupSteps
    from custom_messages.contracts.access_v2 import AccountSharing
    from runtime_settings.instance_layout import InstanceLayout
    from secure_tool_broker.model_key_page import person_model_key_store
    from tests.broker_access_support import access_store, agent_context, linked_gate

    async def _post(_event) -> None:
        return None

    person_model_key_store.cache_clear()
    _fake, base_url = provider
    store = access_store(tmp_path / "access.db")
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    steps = PersonalSetupSteps(store=lambda: store, catalog=CatalogSource(None).current, team_id=TEST_TEAM)
    gateway_setup = GatewayPersonSetupStore(
        access=lambda: store,
        layout=layout,
        team_id=TEST_TEAM,
        personal_setup=GatewayPersonalSetup(steps=steps, events=_GatewayEvents(), emit=_post),
    )
    sharing = TeamAccountSharing(store, TEST_TEAM)
    sharing.choose_model_keys(AccountSharing.PRIVATE, by="admin-1")
    erin = gateway_setup.set_up("erin", PersonSetupChoices(), actor_id="admin-1", new_person=True)
    sharing.choose_model_keys(AccountSharing.SHARED, by="admin-1")
    finn = gateway_setup.set_up("finn", PersonSetupChoices(), actor_id="admin-1", new_person=True)
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=linked_gate(store)).build_app()
    async with TestClient(TestServer(app)) as broker:
        await broker.post(await _page_path(broker), data={"action": "save", "base_url": base_url, "model": MODEL, "key": GOOD_KEY})

        async def lookup(person: str) -> dict:
            body = {"address": base_url, "context": agent_context(person), "purpose": "chat"}
            return await (await broker.post(MODEL_KEY_LOOKUP_ROUTE, headers=caller_headers(), json=body)).json()

        erin_key, finn_key = await lookup("erin"), await lookup("finn")

    assert (erin.may_use_team_keys, finn.may_use_team_keys) == (False, True)
    # Both lookups ran after the team shared its keys again: people already
    # added keep the switch they were given.
    assert (erin_key["team_keys_allowed"], erin_key["key"]) == (False, "")
    assert finn_key["key"] == GOOD_KEY


@pytest.mark.asyncio
async def test_agents_can_list_saved_endpoints_with_the_address_that_answered_but_never_the_key(broker, provider) -> None:
    from secure_tool_broker.defaults import MODEL_KEY_ENDPOINTS_ROUTE

    _fake, base_url = provider
    host = base_url.removesuffix("/v1")
    page = await _page_path(broker)
    await broker.post(page, data={"form_id": "key-form", "preset": "new", "action": "save", "base_url": host, "model": MODEL, "key": GOOD_KEY})

    listed = await broker.get(MODEL_KEY_ENDPOINTS_ROUTE, headers=caller_headers(agent_caller_label("TeamArchitect")))
    text = await listed.text()

    assert listed.status == 200 and GOOD_KEY not in text
    [endpoint] = json.loads(text)["endpoints"]
    assert endpoint["base_url"] == base_url and endpoint["tested_model_id"] == MODEL


@pytest.mark.asyncio
async def test_a_saved_key_for_another_endpoint_is_offered_with_how_it_was_tested(broker, provider) -> None:
    _fake, base_url = provider
    host = base_url.removesuffix("/v1")
    page = await _page_path(broker)
    await broker.post(page, data={"form_id": "key-form", "preset": "new", "action": "save", "base_url": host, "model": MODEL, "key": GOOD_KEY})

    text = await (await broker.get(page)).text()

    assert f'data-base-url="{base_url}"' in text and ">Saved key: 127.0.0.1" in text
    assert f'data-model="{MODEL}" data-api-mode="chat_completions"' in text
    # While a test runs the page says so, and the button cannot be pressed twice.
    assert "data-model-key-progress hidden>Sending one test request to the endpoint." in text
    assert 'button.textContent = progress ? "Testing' in text
