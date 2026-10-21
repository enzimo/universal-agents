from __future__ import annotations

from tests.browser_assets import module_source

import asyncio
import errno
import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.onboarding_web import provider_test
from model_providers import connection_test
from assistant_gateway.onboarding_web.server import (
    OnboardingWebService,
    OnboardingWebSettings,
)
from assistant_gateway.onboarding_engine import (
    normalize_onboarding_answers,
)
from custom_agents.xai_oauth_auth import XAIDeviceAuthorization, XAIOAuthCredentials


def _authorization() -> XAIDeviceAuthorization:
    return XAIDeviceAuthorization(
        device_code="server-only-device-code",
        user_code="ABCD-EFGH",
        verification_uri="https://auth.x.ai/activate",
        verification_uri_complete="https://auth.x.ai/activate?user_code=ABCD-EFGH",
        expires_in=900,
        interval=1,
        token_endpoint="https://auth.x.ai/oauth2/token",
        device_authorization_endpoint="https://auth.x.ai/oauth2/device/code",
        requested_at_epoch_seconds=1_900_000_000.0,
    )


def test_onboarding_discards_browser_token_for_xai_oauth(tmp_path: Path) -> None:
    service = OnboardingWebService(
        OnboardingWebSettings(
            instance_root=str(tmp_path / "instance"), restart_on_complete=False
        )
    )

    answers = normalize_onboarding_answers(
        {
            "instance_root": str(tmp_path / "instance"),
            "model_provider": "xAI Grok OAuth",
            "model_token": "stale-token-from-another-provider",
        },
        service.template,
    )

    assert answers["model_token"] == ""


@pytest.mark.asyncio
async def test_browser_device_flow_never_returns_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.request_xai_device_authorization",
        lambda *args, **kwargs: _authorization(),
    )
    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.poll_xai_device_token",
        lambda *args, **kwargs: {
            "access_token": "browser-must-never-see-access-token",
            "refresh_token": "browser-must-never-see-refresh-token",
            "expires_in": 900,
        },
    )
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        start_response = await client.post(
            "/api/xai-oauth/device-login",
            json={"xai_oauth": {"base_url": "https://api.x.ai/v1"}},
        )
        start_payload = await start_response.json()
        login_id = start_payload["login_id"]
        status_payload = start_payload
        for _ in range(50):
            status_response = await client.get(
                f"/api/xai-oauth/device-login/{login_id}"
            )
            status_payload = await status_response.json()
            if status_payload["status"] != "pending":
                break
            await asyncio.sleep(0.01)

    serialized_responses = json.dumps([start_payload, status_payload])
    assert start_response.status == 202
    assert start_response.headers["Cache-Control"] == "no-store"
    assert status_response.headers["Cache-Control"] == "no-store"
    assert start_payload["verification_uri_complete"].startswith("https://auth.x.ai/")
    assert "device_code" not in start_payload
    assert "browser-must-never-see" not in serialized_responses
    assert "auth_store_path" not in status_payload
    assert status_payload["status"] == "complete"
    auth_store = instance_root / "auth" / "xai_oauth_auth.json"
    stored = json.loads(auth_store.read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] == "browser-must-never-see-access-token"


@pytest.mark.asyncio
async def test_browser_device_flow_explains_a_store_that_cannot_be_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The person sees the lock problem itself, not a generic or "sign in again" failure."""

    def refuse(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.request_xai_device_authorization",
        lambda *args, **kwargs: _authorization(),
    )
    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.poll_xai_device_token",
        lambda *args, **kwargs: {"access_token": "new-access-token", "refresh_token": "new-refresh-token"},
    )
    monkeypatch.setattr("file_locking.fcntl.flock", refuse)
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        start_response = await client.post(
            "/api/xai-oauth/device-login",
            json={"xai_oauth": {"base_url": "https://api.x.ai/v1"}},
        )
        login_id = (await start_response.json())["login_id"]
        for _ in range(50):
            status_payload = await (await client.get(f"/api/xai-oauth/device-login/{login_id}")).json()
            if status_payload["status"] != "pending":
                break
            await asyncio.sleep(0.01)

    assert status_payload["status"] == "error"
    assert status_payload["code"] == "xai_oauth_auth_store_lock_unavailable"
    assert status_payload["relogin_required"] is False
    assert "could not be locked" in status_payload["message"]
    assert "supports file locks" in status_payload["message"]
    assert not (instance_root / "auth" / "xai_oauth_auth.json").exists()


@pytest.mark.asyncio
async def test_browser_device_flow_rejects_store_outside_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request_called = False

    def fail_if_called(*args: object, **kwargs: object) -> XAIDeviceAuthorization:
        nonlocal request_called
        request_called = True
        return _authorization()

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.request_xai_device_authorization",
        fail_if_called,
    )
    service = OnboardingWebService(
        OnboardingWebSettings(
            instance_root=str(tmp_path / "instance"), restart_on_complete=False
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/xai-oauth/device-login",
            json={
                "xai_oauth": {
                    "base_url": "https://api.x.ai/v1",
                    "auth_store_path": str(tmp_path / "outside" / "xai.json"),
                }
            },
        )
        payload = await response.json()

    assert response.status == 502
    assert payload["code"] == "xai_oauth_store_outside_instance"
    assert request_called is False


@pytest.mark.asyncio
async def test_browser_provider_test_rejects_store_outside_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_test_called = False

    async def fail_if_called(*args: object, **kwargs: object) -> object:
        nonlocal provider_test_called
        provider_test_called = True
        raise AssertionError("provider test must not run with an outside auth store")

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.provider_process.run_provider_check",
        fail_if_called,
    )
    service = OnboardingWebService(
        OnboardingWebSettings(
            instance_root=str(tmp_path / "instance"), restart_on_complete=False
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/provider-test",
            json={
                "provider": "xai-oauth",
                "endpoint": "https://api.x.ai/v1",
                "model": "grok-4.5",
                "xai_oauth": {
                    "auth_store_path": str(tmp_path / "outside" / "xai.json")
                },
            },
        )
        payload = await response.json()

    assert response.status == 502
    assert payload["code"] == "xai_oauth_store_outside_instance"
    assert provider_test_called is False


def test_provider_connection_resolves_server_side_xai_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_authorization = ""

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.provider_test.resolve_xai_oauth_credentials",
        lambda *args, **kwargs: XAIOAuthCredentials(
            provider="xai-oauth",
            base_url="https://api.x.ai/v1",
            api_key="server-side-bearer",
            source="auth_store",
            auth_mode="oauth_device_code",
        ),
    )

    class StreamResponse:
        status = 200

        def __enter__(self) -> "StreamResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, _: int = -1) -> bytes:
            return (
                'data: {"type":"response.output_text.delta","delta":"connected"}\n\n'
                'data: {"type":"response.completed","response":{"id":"resp_1"}}\n\n'
            ).encode("utf-8")

    def fake_urlopen(request: object, timeout: float) -> StreamResponse:
        nonlocal captured_authorization
        captured_authorization = request.headers["Authorization"]
        return StreamResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="xAI Grok OAuth",
        endpoint="https://api.x.ai/v1",
        token="browser-token-must-be-ignored",
        model="grok-4.5",
        instance_root=tmp_path,
    )

    assert result.ok is True
    assert result.sample == "connected"
    assert captured_authorization == "Bearer server-side-bearer"


def test_onboarding_assets_include_xai_oauth_device_flow_and_real_countdown() -> None:
    asset_root = Path("assistant_gateway/onboarding_web/assets")
    app_text = module_source(asset_root, "app.js")
    styles_text = (asset_root / "styles.css").read_text(encoding="utf-8")

    assert 'label: "xAI Grok OAuth"' in app_text
    assert 'defaultModel: "grok-4.5"' in app_text
    assert "function runtimeTokenForSelectedProvider()" in app_text
    assert 'requestClient.fetch("/api/xai-oauth/device-login"' in app_text
    assert 'requestClient.fetch("/api/xai-oauth/device-login/"' in app_text
    assert "state.xaiDeviceExpiresAt - Date.now() / 1000" in app_text
    assert "Access and refresh tokens stay on the onboarding server" in app_text
    assert ".xai-oauth-login-status.is-success" in styles_text
