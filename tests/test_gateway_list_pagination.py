"""Browser lists and changes through authenticated HTTP handlers and real SQLite stores."""

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp, _config
from tests.test_gateway_phase1 import gateway_signed_commands  # noqa: F401 - fixture


@pytest.mark.asyncio
@pytest.mark.usefixtures("gateway_signed_commands")
async def test_memory_creation_through_http_manager_and_real_store(
    tmp_path, monkeypatch
):
    from tests.test_gateway_phase1 import _attach_in_process_manager_control

    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_memory_test")
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
    )
    _attach_in_process_manager_control(gateway, config)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    async with TestClient(TestServer(app.build_app())) as client:
        assert (
            await client.post("/api/web-chat/memories/personal", json={})
        ).status == 401
        auth = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_memory_test"}
        )
        csrf = (await auth.json())["permission_csrf_token"]
        session = (await (await client.post("/api/web-chat/sessions")).json())[
            "session"
        ]["session_key"]
        headers = {
            "X-UA-Permission-CSRF": csrf,
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
        }
        for scope in ("personal", "team"):
            url = f"/api/web-chat/memories/{scope}"
            body = {
                "session_key": session,
                "fact_text": f"New {scope} fact",
                "tags": ["ui", "test"],
                "invalid_after_utc": "2099-01-01T12:00:00Z",
            }
            assert (await client.post(url, json=body)).status == 403
            for invalid in (
                {"fact_text": " "},
                {"tags": "bad"},
                {"invalid_after_utc": "bad"},
            ):
                assert (
                    await client.post(url, headers=headers, json={**body, **invalid})
                ).status == 400
            created = await client.post(
                url, headers=headers, json={**body, "canonical_user_id": "other-user"}
            )
            assert created.status == 200, await created.text()
            result = await created.json()
            assert result["action"] == "create"
            assert result["memory"]["fact_text"] == body["fact_text"]
            assert result["memory"]["tags"] == ["test", "ui"]
            listing = await client.get(
                "/api/web-chat/memories",
                params={"session_key": session, "scope": scope},
            )
            payload = await listing.json()
            assert payload["can_create"] is True
            assert result["memory"]["memory_id"] in [
                item["memory_id"] for item in payload["memories"]
            ]

        # Removing administrator membership changes both the offered action and
        # the manager's write decision, even if the browser still shows New.
        gateway.identity_repository.create_team_membership(
            canonical_user_id=config.user.canonical_user_id,
            team_id=config.gateway.team_id,
            role="member",
        )
        listing = await client.get(
            "/api/web-chat/memories", params={"session_key": session, "scope": "team"}
        )
        assert (await listing.json())["can_create"] is False
        denied = await client.post(
            "/api/web-chat/memories/team", headers=headers, json=body
        )
        assert denied.status == 403, await denied.text()
    await gateway.close()
