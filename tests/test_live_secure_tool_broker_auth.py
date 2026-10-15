"""Opt-in live checks for secure tool broker authentication flows.

These tests execute real local credential CLIs against a temporary broker-owned
state directory. They may still authenticate external accounts and are skipped
unless RUN_LIVE_SECURE_TOOL_BROKER_AUTH=1 is set.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import agent_context, caller_headers, gateway_headers, open_gate


LIVE_ROOT = "live-test-token"
DUMMY_GITHUB_TOKEN = "ghp_dummy_token_for_live_test"
DUMMY_BITWARDEN_SESSION = "dummy-bw-session-for-live-test"
LIVE_USER = "live-secure-broker-user"
# People acting directly (sign-in, status) come through the gateway.
LIVE_TRUSTED_CONTEXT = {
    "canonical_user_id": LIVE_USER,
    "principal_type": "user",
}
# Agent tool use needs a signed task context naming the same person.
LIVE_AGENT_CONTEXT = agent_context(LIVE_USER)

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_SECURE_TOOL_BROKER_AUTH") != "1",
        reason="Set RUN_LIVE_SECURE_TOOL_BROKER_AUTH=1 to run live secure broker auth tests.",
    ),
]


@pytest.fixture(autouse=True)
def _isolated_broker_owner_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep live auth probes out of the operator's persistent CLI auth stores."""

    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIALS_DIR",
        str(tmp_path / "broker_credentials"),
    )


def _github_token() -> str:
    return (
        os.getenv("LIVE_SECURE_TOOL_BROKER_GH_TOKEN")
        or os.getenv("GH_TOKEN")
        or os.getenv("GITHUB_TOKEN")
        or ""
    ).strip()


def _bitwarden_session() -> str:
    return (os.getenv("LIVE_SECURE_TOOL_BROKER_BW_SESSION") or os.getenv("BW_SESSION") or "").strip()


def _submit_path(login_url: str) -> str:
    return "/" + login_url.split("/", 3)[3]


@pytest.mark.asyncio
async def test_live_secure_tool_broker_reports_github_auth_status() -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed or not on PATH.")

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/v1/auth/status/github",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["provider"] == "github"
    assert payload["status"] in {"authenticated", "auth_required", "unavailable"}
    assert isinstance(payload["authenticated"], bool)
    assert "message" in payload
    assert "token" in payload["auth_options"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "executable"),
    [
        ("onepassword", "op"),
        ("keepass", "keepassxc-cli"),
        ("vault", "vault"),
    ],
)
async def test_live_secure_tool_broker_reports_additional_credential_provider_status(provider: str, executable: str) -> None:
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} is not installed or not on PATH.")

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            f"/v1/auth/status/{provider}",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["provider"] == provider
    assert payload["status"] in {"authenticated", "configured", "auth_required", "unavailable"}
    assert isinstance(payload["authenticated"], bool)
    assert "message" in payload


@pytest.mark.asyncio
async def test_live_secure_tool_broker_github_token_form_rejects_dummy_token() -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed or not on PATH.")

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create_response = await client.post(
            "/v1/auth/github/sessions",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        create_payload = await create_response.json()

        submit_response = await client.post(
            _submit_path(create_payload["login_url"]),
            data={"token": DUMMY_GITHUB_TOKEN, "hostname": "github.com"},
        )
        text = await submit_response.text()

    assert create_response.status == 200
    assert submit_response.status == 400
    assert "GitHub authentication failed" in text


@pytest.mark.asyncio
async def test_live_secure_tool_broker_google_token_form_rejects_invalid_json() -> None:
    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create_response = await client.post(
            "/v1/auth/google/sessions",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT, "method": "json_token"},
        )
        create_payload = await create_response.json()

        submit_response = await client.post(
            _submit_path(create_payload["login_url"]),
            data={"token_json": "not-json"},
        )
        text = await submit_response.text()

    assert create_response.status == 200
    assert submit_response.status == 400
    assert "Google authentication failed" in text


@pytest.mark.asyncio
async def test_live_secure_tool_broker_bitwarden_form_rejects_missing_unlock_password() -> None:
    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create_response = await client.post(
            "/v1/auth/bitwarden/sessions",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        create_payload = await create_response.json()

        submit_response = await client.post(
            _submit_path(create_payload["login_url"]),
            data={"client_id": "dummy-client-id", "client_secret": "dummy-client-secret"},
        )
        text = await submit_response.text()

    assert create_response.status == 200
    assert submit_response.status == 400
    assert "Bitwarden authentication failed" in text
    assert "Bitwarden master password is required." in text


@pytest.mark.asyncio
async def test_live_secure_tool_broker_github_token_form_authenticates_with_real_token() -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed or not on PATH.")
    token = _github_token()
    if not token or token == DUMMY_GITHUB_TOKEN:
        pytest.skip(
            "Set LIVE_SECURE_TOOL_BROKER_GH_TOKEN, GH_TOKEN, or GITHUB_TOKEN to a real token "
            "to run live GitHub token login."
        )

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create_response = await client.post(
            "/v1/auth/github/sessions",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        create_payload = await create_response.json()

        submit_response = await client.post(
            _submit_path(create_payload["login_url"]),
            data={"token": token, "hostname": "github.com"},
        )

        status_response = await client.post(
            "/v1/auth/status/github",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        status_payload = await status_response.json()

    assert create_response.status == 200
    assert submit_response.status == 200
    assert status_response.status == 200
    assert status_payload["status"] == "authenticated"
    assert status_payload["authenticated"] is True


@pytest.mark.asyncio
async def test_live_secure_tool_broker_bitwarden_credential_search_real_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("bw") is None:
        pytest.skip("bw is not installed or not on PATH.")
    bw_session = _bitwarden_session()
    if not bw_session or bw_session == DUMMY_BITWARDEN_SESSION:
        pytest.skip("Set LIVE_SECURE_TOOL_BROKER_BW_SESSION or BW_SESSION to run live Bitwarden search.")
    query = os.getenv("LIVE_SECURE_TOOL_BROKER_BW_QUERY", "github").strip()
    monkeypatch.setenv("BW_SESSION", bw_session)

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        search_response = await client.post(
            "/v1/tools/bitwarden",
            headers=caller_headers(root=LIVE_ROOT),
            json={
                "provider": "bitwarden",
                "action": "credential.search",
                "params": {"query": query, "limit": 10},
                "reason": "live Bitwarden credential metadata search test",
                "context": LIVE_AGENT_CONTEXT,
            },
        )
        search_payload = await search_response.json()

    serialized = str(search_payload)
    assert search_response.status == 200
    assert search_payload["status"] == "success"
    assert "credentials" in search_payload["content"]
    assert bw_session not in serialized


@pytest.mark.asyncio
async def test_live_secure_tool_broker_github_login_from_bitwarden_real_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed or not on PATH.")
    if shutil.which("bw") is None:
        pytest.skip("bw is not installed or not on PATH.")
    bw_session = _bitwarden_session()
    if not bw_session or bw_session == DUMMY_BITWARDEN_SESSION:
        pytest.skip("Set LIVE_SECURE_TOOL_BROKER_BW_SESSION or BW_SESSION to run live Bitwarden automation.")
    item_id = os.getenv("LIVE_SECURE_TOOL_BROKER_BW_GH_ITEM_ID", "").strip()
    item_name = os.getenv("LIVE_SECURE_TOOL_BROKER_BW_GH_ITEM_NAME", "").strip()
    if not item_id and not item_name:
        pytest.skip("Set LIVE_SECURE_TOOL_BROKER_BW_GH_ITEM_ID or LIVE_SECURE_TOOL_BROKER_BW_GH_ITEM_NAME.")
    field = os.getenv("LIVE_SECURE_TOOL_BROKER_BW_GH_FIELD", "password").strip() or "password"
    monkeypatch.setenv("BW_SESSION", bw_session)

    params = {"credential_provider": "bitwarden", "field_ref": field, "hostname": "github.com"}
    if item_id:
        params["item_id"] = item_id
    else:
        params["item_name"] = item_name

    service = SecureToolBrokerService(token=LIVE_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        login_response = await client.post(
            "/v1/tools/github",
            headers=caller_headers(root=LIVE_ROOT),
            json={
                "provider": "github",
                "action": "auth.login.credential",
                "params": params,
                "reason": "live Bitwarden-backed GitHub auth test",
                "context": LIVE_AGENT_CONTEXT,
            },
        )
        login_payload = await login_response.json()

        status_response = await client.post(
            "/v1/auth/status/github",
            headers=gateway_headers(root=LIVE_ROOT),
            json={"context": LIVE_TRUSTED_CONTEXT},
        )
        status_payload = await status_response.json()

    assert login_response.status == 200
    assert login_payload["status"] == "success"
    assert login_payload["content"]["credential_source"] == "bitwarden"
    assert status_response.status == 200
    assert status_payload["status"] == "authenticated"
