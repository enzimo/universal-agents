"""Tests for the secure credential collection form expiry UI, validation, and card view (A1, A2)."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog import (
    CredentialOwnerScope,
    collect_credential,
    load_catalog,
)
from secure_tool_broker.credential_collection_form import (
    CREDENTIAL_FORM_ID,
    DELETE_AUTOMATICALLY_FIELD,
    EXPIRY_CUSTOM_LOCAL_FIELD,
    EXPIRY_TIMEZONE_FIELD,
    EXPIRY_UTC_OFFSET_FIELD,
    EXPIRY_WHEN_FIELD,
)
from secure_tool_broker.credential_expiry import (
    CUSTOM_EXPIRY_OPTION,
    EXPIRY_PRESET_OPTIONS,
    format_utc_timestamp,
)
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.broker_pages import card_view_url
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers, open_gate

USER_ID = "form-expiry-user"
OWNER_SCOPE = CredentialOwnerScope.user(USER_ID)
WEB_CHAT_ORIGIN = "http://chat.example.test:8080"


def _trusted_context() -> dict[str, Any]:
    return {
        "canonical_user_id": USER_ID,
        "principal_type": "user",
        "team_id": TEST_TEAM,
    }


def _collection_request(
    *,
    credential_key: str = "demo_service_token",
    store_target: str = "sealbox",
    expires_at: str | None = None,
    flow: str = "credential_collection",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "credential_key": credential_key,
        "service_name": "Demo Service",
        "purpose": "Temporary API access",
        "store_target": store_target,
        "flow": flow,
        "fields": [
            {
                "name": "username",
                "kind": "username",
                "visibility": "metadata_visible",
                "value_source": "user_input",
                "required": True,
            },
            {
                "name": "api_token",
                "kind": "token",
                "visibility": "secret",
                "value_source": "user_input",
                "required": True,
            },
        ],
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    return payload


@pytest.fixture(autouse=True)
def _isolated_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", lambda **_kwargs: {})
    commands: list[list[str]] = []

    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)
    return commands


async def _create_session(client: TestClient, request_payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/v1/credential-collection/sessions",
        headers=gateway_headers(),
        json={"context": _trusted_context(), "request": request_payload},
    )
    assert response.status == 200, await response.text()
    data: dict[str, Any] = await response.json()
    return data


def _form_path(created: dict[str, Any]) -> str:
    return "/" + str(created["form_url"]).split("/", 3)[3]


def _card_outcome(html_text: str) -> str | None:
    match = re.search(r'data-card-outcome="([a-z]+)"', html_text)
    return match.group(1) if match else None


@pytest.mark.asyncio
async def test_form_renders_expiry_controls_on_standalone_and_card_view() -> None:
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, _collection_request())
        path = _form_path(created)

        for target_url, is_card in ((path, False), (card_view_url(path), True)):
            resp = await client.get(target_url)
            assert resp.status == 200
            page = await resp.text()
            assert f'id="{CREDENTIAL_FORM_ID}" action="#{CREDENTIAL_FORM_ID}"' in page
            assert f'name="{DELETE_AUTOMATICALLY_FIELD}"' in page
            assert "Delete automatically" in page
            assert f'name="{EXPIRY_WHEN_FIELD}"' in page
            assert f'name="{EXPIRY_CUSTOM_LOCAL_FIELD}"' in page
            assert f'name="{EXPIRY_TIMEZONE_FIELD}"' in page
            assert f'name="{EXPIRY_UTC_OFFSET_FIELD}"' in page
            assert "data-credential-expiry-control" in page
            for value, label in EXPIRY_PRESET_OPTIONS:
                assert f'value="{value}"' in page
                assert f">{label}</option>" in page
            # Unticked by default: When select and custom datetime-local start hidden and disabled.
            assert 'data-expiry-checkbox>' in page
            assert "data-expiry-when-wrap hidden>" in page
            assert "data-expiry-custom-wrap hidden>" in page
            # Key does not exist yet: no existing-key warning.
            assert "including earlier versions" not in page
            if is_card:
                assert 'class="card-view"' in page
                assert "frame-ancestors" in resp.headers["Content-Security-Policy"]
            else:
                assert 'class="card-view"' not in page


@pytest.mark.asyncio
async def test_form_prefills_proposed_expiry_and_warns_when_key_already_exists() -> None:
    # Seed an existing credential so the existing-key warning appears.
    collect_credential(
        _collection_request(),
        {"username": "old-user", "api_token": "old-secret"},
        scope=OWNER_SCOPE,
        allow_expiry_change=True,
    )

    proposed = format_utc_timestamp(datetime.now(timezone.utc) + timedelta(hours=2))
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, _collection_request(expires_at=proposed))
        page = await (await client.get(_form_path(created))).text()
        assert f'data-proposed-expires-at="{proposed}"' in page
        assert "data-expiry-checkbox checked>" in page
        assert f'value="{CUSTOM_EXPIRY_OPTION}" selected>' in page
        assert "data-expiry-when-wrap>" in page
        assert "data-expiry-custom-wrap>" in page
        assert "This credential already exists." in page
        assert "including earlier versions, is deleted at that time." in page


@pytest.mark.asyncio
async def test_form_end_to_end_stores_ttl_and_overwrite_without_expiry_clears_it(
    _isolated_broker: list[list[str]],
) -> None:
    commands = _isolated_broker
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        # 1. Submit via inline card view with preset "15m" -> Sealbox gets --ttl, catalog gets expires_at.
        created = await _create_session(client, _collection_request())
        card_url = card_view_url(_form_path(created))
        submit = await client.post(
            card_url,
            data={
                "field__username": "alice",
                "field__api_token": "TEMP-SECRET-VALUE",
                DELETE_AUTOMATICALLY_FIELD: "yes",
                EXPIRY_WHEN_FIELD: "15m",
            },
        )
        assert submit.status == 200
        submit_html = await submit.text()
        assert _card_outcome(submit_html) == "finished"
        assert "TEMP-SECRET-VALUE" not in submit_html

        assert len(commands) == 1
        ttl_idx = commands[0].index("--ttl")
        ttl_val = int(commands[0][ttl_idx + 1])
        assert 895 <= ttl_val <= 901

        saved = load_catalog(scope=OWNER_SCOPE).credentials["demo_service_token"]
        assert saved.expires_at is not None
        assert "." not in saved.expires_at  # Whole-second UTC timestamp (C13).

        status_resp = await client.get(
            f"/v1/credential-collection/sessions/{created['session_id']}",
            headers=gateway_headers(),
        )
        status_data = await status_resp.json()
        assert status_data["status"] == "credential_stored"
        assert status_data["result"]["content"]["expires_at"] == saved.expires_at
        assert status_data["result"]["content"]["metadata"]["expires_at"] == saved.expires_at

        # 2. Submit a new form over the same key with proposed expires_at UNTICKED -> clears expiry (A1, A2).
        commands.clear()
        proposed = format_utc_timestamp(datetime.now(timezone.utc) + timedelta(hours=3))
        created2 = await _create_session(client, _collection_request(expires_at=proposed))
        submit2 = await client.post(
            _form_path(created2),
            data={
                "field__username": "alice",
                "field__api_token": "LASTING-SECRET-VALUE",
                # delete_automatically omitted (person unticked it)
                EXPIRY_WHEN_FIELD: CUSTOM_EXPIRY_OPTION,
            },
        )
        assert submit2.status == 200
        assert len(commands) == 1
        assert "--ttl" not in commands[0]
        updated = load_catalog(scope=OWNER_SCOPE).credentials["demo_service_token"]
        assert updated.expires_at is None


@pytest.mark.asyncio
async def test_form_refuses_past_under_five_minutes_overflow_and_non_sealbox_and_allows_retry(
    _isolated_broker: list[list[str]],
) -> None:
    commands = _isolated_broker
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, _collection_request())
        card_url = card_view_url(_form_path(created))

        now_utc = datetime.now(timezone.utc)
        too_soon_utc = (now_utc + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
        past_utc = (now_utc - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")

        for bad_local, offset, expected_fragment in (
            (past_utc, "0", "at least 5 minutes"),
            (too_soon_utc, "0", "at least 5 minutes"),
            ("9999-12-31T23:59", "720", "valid deletion date and time"),
            ("2026-12-01T10:00", "", "time zone"),
        ):
            bad = await client.post(
                card_url,
                data={
                    "field__username": "keep-this-user",
                    "field__api_token": "DO-NOT-ECHO-THIS-SECRET",
                    DELETE_AUTOMATICALLY_FIELD: "yes",
                    EXPIRY_WHEN_FIELD: CUSTOM_EXPIRY_OPTION,
                    EXPIRY_CUSTOM_LOCAL_FIELD: bad_local,
                    EXPIRY_UTC_OFFSET_FIELD: offset,
                },
            )
            assert bad.status == 400
            html_body = await bad.text()
            assert 'class="status status-error" role="alert"' in html_body
            assert "Nothing was saved." in html_body
            assert expected_fragment in html_body
            # Non-secret field preserved; secret field cleared; expiry controls preserved; card stays open.
            assert 'value="keep-this-user"' in html_body
            assert "DO-NOT-ECHO-THIS-SECRET" not in html_body
            assert "data-expiry-checkbox checked>" in html_body
            assert f'value="{bad_local}"' in html_body
            assert _card_outcome(html_body) is None
            assert commands == []

        # Retrying the SAME session with a valid custom local time (UTC+2, offset -120) succeeds!
        future_utc = (now_utc + timedelta(hours=2)).replace(second=0, microsecond=0)
        future_local_plus2 = (future_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        ok = await client.post(
            card_url,
            data={
                "field__username": "keep-this-user",
                "field__api_token": "FINAL-SECRET-VALUE",
                DELETE_AUTOMATICALLY_FIELD: "yes",
                EXPIRY_WHEN_FIELD: CUSTOM_EXPIRY_OPTION,
                EXPIRY_CUSTOM_LOCAL_FIELD: future_local_plus2,
                EXPIRY_UTC_OFFSET_FIELD: "-120",
            },
        )
        assert ok.status == 200
        assert _card_outcome(await ok.text()) == "finished"
        saved = load_catalog(scope=OWNER_SCOPE).credentials["demo_service_token"]
        assert saved.expires_at == format_utc_timestamp(future_utc)
        assert len(commands) == 1
        assert "--ttl" in commands[0]

        # Non-Sealbox store target refuses expiry at the form with red error and lets the user untick & retry.
        bw_created = await _create_session(
            client,
            _collection_request(credential_key="bw_key", store_target="bitwarden"),
        )
        bw_path = _form_path(bw_created)
        bw_bad = await client.post(
            bw_path,
            data={
                "field__username": "bw-user",
                "field__api_token": "BW-SECRET",
                DELETE_AUTOMATICALLY_FIELD: "yes",
                EXPIRY_WHEN_FIELD: "1h",
            },
        )
        assert bw_bad.status == 400
        bw_html = await bw_bad.text()
        assert 'class="status status-error" role="alert"' in bw_html
        assert "Sealbox" in bw_html
        assert "BW-SECRET" not in bw_html
        assert 'value="bw-user"' in bw_html


@pytest.mark.asyncio
async def test_session_creation_rejects_expires_at_on_deletion_flow() -> None:
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        resp = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": _trusted_context(),
                "request": _collection_request(
                    flow="credential_deletion",
                    expires_at="2026-12-01T12:00:00Z",
                ),
            },
        )
        assert resp.status == 400
