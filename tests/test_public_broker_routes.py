"""Keep deployment routing aligned with the broker's real browser endpoints."""

from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime_settings.network import rewrite_url_to_public_base
from secure_tool_broker.credential_catalog import load_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import gateway_headers, open_gate
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    link_request,
    new_credential_form,
    save_credentials,
    sealbox,
    token_credential,
    world,
)


ROOT = Path(__file__).resolve().parents[1]


def _documented_broker_prefixes() -> set[str]:
    document = (ROOT / "user_docs_source/reverse-proxy.md").read_text()
    examples = [
        yaml.safe_load(block)
        for block in re.findall(r"```yaml\n(.*?)```", document, re.S)
    ]
    deployment = next(
        example["services"]["ua"]["deploy"]
        for example in examples
        if "services" in example
    )
    labels = dict(label.split("=", 1) for label in deployment["labels"])
    assert int(labels["traefik.http.routers.ua-broker.priority"]) > int(
        labels["traefik.http.routers.ua-web.priority"]
    )
    return set(
        re.findall(
            r"PathPrefix\(`([^`]+)`\)", labels["traefik.http.routers.ua-broker.rule"]
        )
    )


def test_documented_proxy_routes_cover_registered_browser_endpoints():
    app = SecureToolBrokerService(token="test-token", access_gate=open_gate()).build_app()
    browser_paths = {
        resource.canonical
        for resource in app.router.resources()
        if resource.canonical != "/health" and not resource.canonical.startswith("/v1/")
    }
    expected = {"/" + path.split("/")[1] + "/" for path in browser_paths}
    assert _documented_broker_prefixes() == expected
    setup_guide = (ROOT / "docker-compose-setup.html").read_text()
    for prefix in expected:
        assert prefix in setup_guide


def _documented_proxy(broker: TestClient) -> web.Application:
    """A public proxy that sends only the documented broker prefixes to the broker."""

    prefixes = _documented_broker_prefixes()

    async def route_public_request(request):
        # Exercise the documented path selection with real broker HTTP.
        # The website fallback deliberately owns no broker form endpoints.
        if not any(request.path.startswith(prefix) for prefix in prefixes):
            raise web.HTTPNotFound()
        response = await broker.request(
            request.method,
            request.path_qs,
            data=await request.read(),
            headers={"Content-Type": request.content_type},
        )
        return web.Response(
            status=response.status,
            body=await response.read(),
            headers={
                "Content-Type": response.headers["Content-Type"],
                "Cache-Control": response.headers["Cache-Control"],
            },
        )

    public_app = web.Application()
    public_app.router.add_route("*", "/{path:.*}", route_public_request)
    return public_app


@pytest.mark.asyncio
async def test_account_link_form_renders_and_submits_through_documented_route(
    world, monkeypatch  # noqa: F811 - the imported pytest fixture
):
    gate, store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
    )
    service = SecureToolBrokerService(token="test-token", access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as broker:
        created = await broker.post(
            "/v1/account-link/sessions",
            headers=gateway_headers(),
            json=link_request(),
        )
        assert created.status == 200
        payload = await created.json()
        async with TestClient(TestServer(_documented_proxy(broker))) as public:
            form_url = rewrite_url_to_public_base(
                payload["form_url"], public_base_url=str(public.make_url("/"))
            )
            form_path = urlsplit(form_url).path
            form = await public.get(form_path)
            assert form.status == 200
            assert "Link an account to Portainer?" in await form.text()
            assert form.headers["Cache-Control"] == "no-store"
            submitted = await public.post(
                form_path, data={"credential_key": "portainer_token", "confirm": "yes"}
            )
            assert submitted.status == 200
            private_api = await public.get("/v1/account-link/sessions/anything")
            assert private_api.status == 404
        status = await broker.get(
            urlsplit(payload["status_url"]).path,
            headers=gateway_headers(),
        )
        assert (await status.json())["status"] == "account_link_created"
    assert store.list_links("team-1")[0].credential_key == "portainer_token"


@pytest.mark.asyncio
async def test_a_new_credential_is_saved_and_linked_through_documented_route(
    world, sealbox  # noqa: F811 - the imported pytest fixtures
):
    gate, store = world
    service = SecureToolBrokerService(token="test-token", access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as broker:
        created = await broker.post(
            "/v1/account-link/sessions",
            headers=gateway_headers(),
            json=link_request("team", ("owner",)),
        )
        assert created.status == 200
        payload = await created.json()
        async with TestClient(TestServer(_documented_proxy(broker))) as public:
            form_path = urlsplit(payload["form_url"]).path
            form = await public.get(form_path)
            assert form.status == 200
            assert "Add a new credential" in await form.text()
            submitted = await public.post(form_path, data=new_credential_form("routed-team-secret"))
            assert submitted.status == 200
            assert submitted.headers["Cache-Control"] == "no-store"
            assert "routed-team-secret" not in await submitted.text()
        status = await broker.get(
            urlsplit(payload["status_url"]).path,
            headers=gateway_headers(),
        )
        assert (await status.json())["status"] == "account_link_created"
    link = store.list_links("team-1")[0]
    assert (link.owner_kind.value, link.credential_key) == ("team", "http-portainer-team")
    assert set(load_catalog(scope=CredentialOwnerScope.team("team-1")).credentials) == {"http-portainer-team"}
    assert list(sealbox.values()) == ["routed-team-secret"]
