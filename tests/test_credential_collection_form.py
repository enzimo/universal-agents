"""Tests for the secure credential collection form expiry UI, validation, and card view (A1, A2)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
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
    CREDENTIAL_EXPIRY_FORM_SCRIPT,
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
    format_expiry_label,
    format_utc_timestamp,
    resolve_form_expiry,
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


@pytest.mark.asyncio
async def test_c14_failed_store_after_claim_ends_page_without_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="sealbox unavailable")

    monkeypatch.setattr(credential_catalog, "run_sealbox", failing_sealbox)
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, _collection_request())
        path = _form_path(created)
        resp = await client.post(
            path,
            data={
                "field__username": "alice",
                "field__api_token": "DO-NOT-ECHO-SECRET",
            },
        )
        assert resp.status == 400
        page = await resp.text()
        assert 'class="status status-error" role="alert"' in page
        assert "Nothing was saved." in page
        assert "Ask the agent for a new form" in page
        # The session is terminal after the claim, so no form or resubmit button may appear.
        assert "<form" not in page
        assert "<input" not in page
        assert "Store Credential" not in page
        assert "DO-NOT-ECHO-SECRET" not in page

        status_resp = await client.get(
            f"/v1/credential-collection/sessions/{created['session_id']}",
            headers=gateway_headers(),
        )
        status_data = await status_resp.json()
        assert status_data["status"] == "credential_unavailable"


@pytest.mark.asyncio
async def test_c15_existing_active_expiry_notice_and_non_sealbox_hides_expiry_controls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # (a) Existing active temporary credential with no proposed expiry on the new request:
    # box starts unticked, and the expiry area names the current deletion time and explains
    # that saving unticked makes it lasting.
    active_expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).replace(microsecond=0)
    active_iso = format_utc_timestamp(active_expiry)
    collect_credential(
        _collection_request(),
        {"username": "old-user", "api_token": "old-secret"},
        scope=OWNER_SCOPE,
        expires_at=active_expiry,
        allow_expiry_change=True,
    )

    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, _collection_request())
        page = await (await client.get(_form_path(created))).text()
        assert "data-expiry-checkbox>" in page  # unticked when request proposes no expiry
        assert f'data-existing-expires-at="{active_iso}"' in page
        assert format_expiry_label(active_iso) in page
        assert 'Saving with "Delete automatically" unticked keeps this credential' in page
        assert "This credential already exists." in page

        # (b) Non-Sealbox write source omits the checkbox and selects, showing a muted note,
        # while hand-made POSTs with an expiry are still refused and logged (C17).
        bw_created = await _create_session(
            client,
            _collection_request(credential_key="bw_only_key", store_target="bitwarden"),
        )
        bw_path = _form_path(bw_created)
        bw_get = await client.get(bw_path)
        assert bw_get.status == 200
        bw_page = await bw_get.text()
        assert "data-credential-expiry-unsupported" in bw_page
        assert "Automatic deletion is only available for Sealbox credentials" in bw_page
        assert f'name="{DELETE_AUTOMATICALLY_FIELD}"' not in bw_page
        assert f'name="{EXPIRY_WHEN_FIELD}"' not in bw_page

        with caplog.at_level(logging.WARNING, logger="secure_tool_broker"):
            bw_post = await client.post(
                bw_path,
                data={
                    "field__username": "bw-user",
                    "field__api_token": "BW-SECRET",
                    DELETE_AUTOMATICALLY_FIELD: "yes",
                    EXPIRY_WHEN_FIELD: "1h",
                },
            )
        assert bw_post.status == 400
        assert any(
            "event=credential_collection_expiry_refused" in rec.message
            and "error_code=credential_expiry_unsupported_source" in rec.message
            for rec in caplog.records
        )


@pytest.mark.asyncio
async def test_c17_secret_true_field_never_echoed_on_validation_error() -> None:
    raw_request = {
        "credential_key": "raw_secret_flag_key",
        "service_name": "Raw Service",
        "purpose": "Secret flag parity",
        "store_target": "sealbox",
        "fields": [
            {
                "name": "account_name",
                "kind": "custom",
                "visibility": "metadata_visible",
                "value_source": "user_input",
                "required": True,
            },
            {
                "name": "opaque_key",
                "kind": "custom",
                "visibility": "metadata_visible",
                "secret": True,
                "value_source": "user_input",
                "required": True,
            },
        ],
    }
    service = SecureToolBrokerService(
        token=BROKER_ROOT,
        access_gate=open_gate(),
        web_chat_origin=WEB_CHAT_ORIGIN,
    )
    async with TestClient(TestServer(service.build_app())) as client:
        created = await _create_session(client, raw_request)
        bad = await client.post(
            _form_path(created),
            data={
                "field__account_name": "visible-account",
                "field__opaque_key": "NEVER-ECHO-SECRET-TRUE-VALUE",
                DELETE_AUTOMATICALLY_FIELD: "yes",
                EXPIRY_WHEN_FIELD: CUSTOM_EXPIRY_OPTION,
                EXPIRY_CUSTOM_LOCAL_FIELD: "2020-01-01T00:00",
                EXPIRY_UTC_OFFSET_FIELD: "0",
            },
        )
        assert bad.status == 400
        body = await bad.text()
        assert 'value="visible-account"' in body
        assert "NEVER-ECHO-SECRET-TRUE-VALUE" not in body


def test_c16_expiry_form_script_prefill_offsets_existing_label_and_dst_gap_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the credential expiry form script")

    match = re.search(r"<script>(.*)</script>", CREDENTIAL_EXPIRY_FORM_SCRIPT, re.DOTALL)
    assert match is not None
    script_source = match.group(1)

    harness = r"""
const vm = require("vm");
const scriptSource = process.argv[1];

function makeEl(initial = {}) {
  const listeners = {};
  return {
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    required: false,
    textContent: "",
    attrs: {},
    ...initial,
    getAttribute(name) { return this.attrs[name] ?? null; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatch(type) { for (const fn of (listeners[type] || [])) fn({}); },
  };
}

function runScenario({ proposed = "", existing = "", checked = false, when = "1h" }) {
  const checkbox = makeEl({ checked });
  const whenWrap = makeEl();
  const whenSelect = makeEl({ value: when });
  const customWrap = makeEl();
  const customInput = makeEl();
  const tzInput = makeEl();
  const offsetInput = makeEl();
  const existingText = makeEl({ textContent: "fallback" });
  const existingNote = existing ? makeEl({ attrs: { "data-existing-expires-at": existing } }) : null;
  if (existingNote) {
    existingNote.querySelector = (sel) => sel === "[data-existing-expires-at-text]" ? existingText : null;
  }
  const form = makeEl();
  const root = {
    attrs: proposed ? { "data-proposed-expires-at": proposed } : {},
    getAttribute(name) { return this.attrs[name] ?? null; },
    closest(sel) { return sel === "form" ? form : null; },
    querySelector(sel) {
      return {
        "[data-expiry-checkbox]": checkbox,
        "[data-expiry-when-wrap]": whenWrap,
        "[data-expiry-when]": whenSelect,
        "[data-expiry-custom-wrap]": customWrap,
        "[data-expiry-custom]": customInput,
        "[data-expiry-timezone]": tzInput,
        "[data-expiry-utc-offset]": offsetInput,
        "[data-existing-expires-at]": existingNote,
      }[sel] || null;
    },
  };
  const sandbox = {
    document: { querySelector: (sel) => sel === "[data-credential-expiry-control]" ? root : null },
    Date,
    Intl,
    Math,
    Number,
    String,
    Boolean,
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptSource, sandbox);
  return { checkbox, whenWrap, whenSelect, customWrap, customInput, tzInput, offsetInput, existingText, form };
}

// 1. Unticked initial state + existing active expiry formatted in local time.
const unticked = runScenario({ existing: "2026-07-15T18:30:00Z", checked: false, when: "1h" });
// 2. Proposed UTC timestamp pre-filled in local time, then probe winter, summer, and spring-forward gap.
const prefilled = runScenario({ proposed: "2026-07-15T18:30:00Z", checked: true, when: "custom" });
const prefilledLocal = prefilled.customInput.value;
const prefilledOffset = prefilled.offsetInput.value;

prefilled.customInput.value = "2026-01-15T10:00";
prefilled.customInput.dispatch("change");
const winterOffset = prefilled.offsetInput.value;

prefilled.customInput.value = "2026-07-15T10:00";
prefilled.customInput.dispatch("change");
const summerOffset = prefilled.offsetInput.value;

// Spring-forward nonexistent hour in America/New_York: 2026-03-08T02:30.
prefilled.customInput.value = "2026-03-08T02:30";
prefilled.customInput.dispatch("change");
const gapOffset = prefilled.offsetInput.value;
const gapJsIso = new Date(2026, 2, 8, 2, 30, 0, 0).toISOString();

process.stdout.write(JSON.stringify({
  tz: prefilled.tzInput.value,
  unticked: {
    whenHidden: unticked.whenWrap.hidden,
    whenDisabled: unticked.whenSelect.disabled,
    customHidden: unticked.customWrap.hidden,
    customDisabled: unticked.customInput.disabled,
    customRequired: unticked.customInput.required,
    existingText: unticked.existingText.textContent,
  },
  prefilled: {
    local: prefilledLocal,
    offset: prefilledOffset,
    customHidden: prefilled.customWrap.hidden,
    customDisabled: prefilled.customInput.disabled,
    customRequired: prefilled.customInput.required,
  },
  winterOffset,
  summerOffset,
  gapOffset,
  gapJsIso,
}));
"""
    ny_env = {**os.environ, "TZ": "America/New_York"}
    ny_run = subprocess.run(
        [node, "-e", harness, script_source],
        env=ny_env,
        text=True,
        capture_output=True,
        check=True,
    )
    ny = json.loads(ny_run.stdout)
    assert ny["tz"] == "America/New_York"
    assert ny["unticked"] == {
        "whenHidden": True,
        "whenDisabled": True,
        "customHidden": True,
        "customDisabled": True,
        "customRequired": False,
        "existingText": "2026-07-15 14:30",
    }
    assert ny["prefilled"] == {
        "local": "2026-07-15T14:30",
        "offset": "240",
        "customHidden": False,
        "customDisabled": False,
        "customRequired": True,
    }
    assert ny["winterOffset"] == "300"
    assert ny["summerOffset"] == "240"
    # Nonexistent spring-forward hour (2026-03-08T02:30 NY) resolves to the exact JS Date instant (07:30Z).
    assert ny["gapOffset"] == "300"
    assert ny["gapJsIso"] == "2026-03-08T07:30:00.000Z"
    resolved_gap = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local="2026-03-08T02:30",
        tz_offset_minutes_behind_utc=ny["gapOffset"],
        timezone_name=ny["tz"],
        now=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
    )
    assert resolved_gap == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)

    # Also verify a positive half-hour offset zone (Asia/Kolkata, UTC+5:30 -> offset -330).
    kolkata_run = subprocess.run(
        [node, "-e", harness, script_source],
        env={**os.environ, "TZ": "Asia/Kolkata"},
        text=True,
        capture_output=True,
        check=True,
    )
    kolkata = json.loads(kolkata_run.stdout)
    assert kolkata["tz"] in {"Asia/Kolkata", "Asia/Calcutta"}
    assert kolkata["prefilled"]["local"] == "2026-07-16T00:00"
    assert kolkata["prefilled"]["offset"] == "-330"
    assert kolkata["winterOffset"] == "-330"
    assert kolkata["summerOffset"] == "-330"
    resolved_kolkata = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local=kolkata["prefilled"]["local"],
        tz_offset_minutes_behind_utc=kolkata["prefilled"]["offset"],
        timezone_name=kolkata["tz"],
        now=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert resolved_kolkata == datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc)
