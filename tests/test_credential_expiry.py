"""Unit and contract tests for temporary credential expiry helpers, contracts, and Sealbox TTL storage."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from assistant_gateway.application.credential_workflow import GatewayCredentialWorkflow
from assistant_gateway.application.secure_results import SendOutcome
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
)
from custom_messages.wire import models as wire_models
from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CredentialOwnerScope,
    collect_credential,
    execute_credential_catalog_action,
    load_catalog,
    register_existing_source,
)
from secure_tool_broker.credential_expiry import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_EXPIRY_FORM_ONLY,
    CREDENTIAL_EXPIRY_INVALID,
    CREDENTIAL_EXPIRY_REQUIRED,
    CREDENTIAL_EXPIRY_TOO_FAR,
    CREDENTIAL_EXPIRY_TOO_SOON,
    CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE,
    CUSTOM_EXPIRY_OPTION,
    EXPIRY_PRESET_SECONDS,
    MAX_CAPTURE_EXPIRY_SECONDS,
    CredentialExpiryError,
    capture_expiry_window,
    format_expiry_label,
    format_utc_timestamp,
    is_expired,
    parse_utc_timestamp,
    remaining_ttl_seconds,
    resolve_form_expiry,
    validate_capture_expiry,
    validate_form_expiry,
    validate_storage_expiry,
)
from secure_tools.providers import ToolContext, ValidatedAction

USER_ID = "expiry-test-user"
TOOL_CONTEXT = ToolContext(agent_id="agent-1", canonical_user_id=USER_ID, principal_type="user")
OWNER_SCOPE = CredentialOwnerScope.user(USER_ID)
BASE_NOW = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", lambda **_kwargs: {})


# --- Pure expiry helper tests (C04, C05) --------------------------------------


def test_parse_format_and_is_expired_boundaries() -> None:
    ts = parse_utc_timestamp("2026-06-01T14:30:00+02:00")
    assert ts == datetime(2026, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
    assert format_utc_timestamp(ts) == "2026-06-01T12:30:00Z"
    assert format_expiry_label(ts) == "2026-06-01 12:30 UTC"

    assert is_expired(None, now=ts) is False
    assert is_expired(ts, now=ts - timedelta(seconds=1)) is False
    assert is_expired(ts, now=ts) is True
    assert is_expired(ts, now=ts + timedelta(seconds=1)) is True

    with pytest.raises(CredentialExpiryError) as naive_exc:
        parse_utc_timestamp("2026-06-01T12:30:00")
    assert naive_exc.value.code == CREDENTIAL_EXPIRY_INVALID

    with pytest.raises(CredentialExpiryError) as bad_exc:
        parse_utc_timestamp("not-a-timestamp")
    assert bad_exc.value.code == CREDENTIAL_EXPIRY_INVALID


def test_remaining_ttl_seconds_rounds_up_and_clamps_expired_to_zero() -> None:
    now = BASE_NOW
    assert remaining_ttl_seconds(now + timedelta(milliseconds=100), now=now) == 1
    assert remaining_ttl_seconds(now + timedelta(seconds=10, milliseconds=1), now=now) == 11
    assert remaining_ttl_seconds(now + timedelta(seconds=10), now=now) == 10
    assert remaining_ttl_seconds(now, now=now) == 0
    assert remaining_ttl_seconds(now - timedelta(seconds=5), now=now) == 0


def test_storage_expiry_accepts_short_ttl_while_form_enforces_five_minute_minimum() -> None:
    now = BASE_NOW
    ten_seconds = now + timedelta(seconds=10)
    assert validate_storage_expiry(ten_seconds, now=now) == ten_seconds

    with pytest.raises(CredentialExpiryError) as past_exc:
        validate_storage_expiry(now, now=now)
    assert past_exc.value.code == CREDENTIAL_EXPIRY_TOO_SOON

    four_fifty_nine = now + timedelta(seconds=299)
    with pytest.raises(CredentialExpiryError) as form_short_exc:
        validate_form_expiry(four_fifty_nine, now=now)
    assert form_short_exc.value.code == CREDENTIAL_EXPIRY_TOO_SOON

    five_minutes = now + timedelta(seconds=300)
    assert validate_form_expiry(five_minutes, now=now) == five_minutes


def test_validate_capture_expiry_enforces_required_min_5m_and_max_90d() -> None:
    now = BASE_NOW
    broker_now, earliest, latest = capture_expiry_window(now=now)
    assert broker_now == "2026-03-07T12:00:00Z"
    assert earliest == "2026-03-07T12:05:00Z"
    assert latest == "2026-06-05T12:00:00Z"

    for missing in (None, "", "   "):
        with pytest.raises(CredentialExpiryError) as exc:
            validate_capture_expiry(missing, now=now)
        assert exc.value.code == CREDENTIAL_EXPIRY_REQUIRED
        assert broker_now in exc.value.message
        assert earliest in exc.value.message
        assert latest in exc.value.message

    for bad in ("2026-03-08T12:00:00", "not-a-timestamp", 12345):
        with pytest.raises(CredentialExpiryError) as exc:
            validate_capture_expiry(bad, now=now)  # type: ignore[arg-type]
        assert exc.value.code == CREDENTIAL_EXPIRY_INVALID

    for too_soon in (now - timedelta(seconds=1), now, now + timedelta(seconds=299)):
        with pytest.raises(CredentialExpiryError) as exc:
            validate_capture_expiry(too_soon, now=now)
        assert exc.value.code == CREDENTIAL_EXPIRY_TOO_SOON
        assert broker_now in exc.value.message
        assert earliest in exc.value.message
        assert latest in exc.value.message

    five_minutes = now + timedelta(seconds=300)
    assert validate_capture_expiry(five_minutes, now=now) == five_minutes

    # Sub-second clock: earliest rounds up so passing earliest back always succeeds (C08).
    half_second_now = BASE_NOW.replace(microsecond=500_000)
    sub_now, sub_earliest, sub_latest = capture_expiry_window(now=half_second_now)
    assert sub_now == "2026-03-07T12:00:00Z"
    assert sub_earliest == "2026-03-07T12:05:01Z"
    assert sub_latest == "2026-06-05T12:00:00Z"
    assert validate_capture_expiry(sub_earliest, now=half_second_now) == datetime(
        2026, 3, 7, 12, 5, 1, tzinfo=timezone.utc
    )
    assert validate_capture_expiry(sub_latest, now=half_second_now) == datetime(
        2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc
    )

    # Offset string is normalized to whole-second UTC.
    offset_input = "2026-03-07T14:15:00.987654+02:00"
    assert validate_capture_expiry(offset_input, now=now) == datetime(2026, 3, 7, 12, 15, 0, tzinfo=timezone.utc)

    exact_max = now + timedelta(seconds=MAX_CAPTURE_EXPIRY_SECONDS)
    assert validate_capture_expiry(exact_max, now=now) == exact_max

    with pytest.raises(CredentialExpiryError) as too_far_exc:
        validate_capture_expiry(exact_max + timedelta(seconds=1), now=now)
    assert too_far_exc.value.code == CREDENTIAL_EXPIRY_TOO_FAR
    assert broker_now in too_far_exc.value.message
    assert earliest in too_far_exc.value.message
    assert latest in too_far_exc.value.message


def test_resolve_form_expiry_presets_and_disabled() -> None:
    assert resolve_form_expiry(enabled=False, when="1h", now=BASE_NOW) is None
    for key, seconds in EXPIRY_PRESET_SECONDS.items():
        resolved = resolve_form_expiry(enabled=True, when=key, now=BASE_NOW)
        assert resolved == BASE_NOW + timedelta(seconds=seconds)


def test_resolve_form_expiry_custom_offset_and_iana_across_dst_transitions() -> None:
    # 2026-03-08 02:00 is US spring-forward (EST UTC-5 -> EDT UTC-4).
    # Chosen time is 2026-03-09 10:00 America/New_York -> 14:00 UTC (EDT, JS offset +240).
    via_offset_west = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local="2026-03-09T10:00",
        tz_offset_minutes_behind_utc="240",
        now=BASE_NOW,
    )
    assert via_offset_west == datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)

    via_iana_west = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local="2026-03-09T10:00",
        timezone_name="America/New_York",
        now=BASE_NOW,
    )
    assert via_iana_west == datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)

    # East of UTC: Europe/Berlin after EU spring-forward (2026-03-29): CEST UTC+2 -> JS offset -120.
    via_offset_east = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local="2026-03-30T10:00",
        tz_offset_minutes_behind_utc="-120",
        now=BASE_NOW,
    )
    assert via_offset_east == datetime(2026, 3, 30, 8, 0, tzinfo=timezone.utc)

    # When both offset and IANA name arrive and disagree, the chosen time's browser offset wins (C05).
    offset_wins = resolve_form_expiry(
        enabled=True,
        when=CUSTOM_EXPIRY_OPTION,
        custom_local="2026-03-09T10:00",
        tz_offset_minutes_behind_utc="240",
        timezone_name="Europe/Berlin",
        now=BASE_NOW,
    )
    assert offset_wins == datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)


def test_resolve_form_expiry_refuses_unknown_zone_or_missing_timezone_info() -> None:
    with pytest.raises(CredentialExpiryError) as unknown_zone:
        resolve_form_expiry(
            enabled=True,
            when=CUSTOM_EXPIRY_OPTION,
            custom_local="2026-03-09T10:00",
            timezone_name="Mars/Olympus_Mons",
            now=BASE_NOW,
        )
    assert unknown_zone.value.code == CREDENTIAL_EXPIRY_INVALID

    with pytest.raises(CredentialExpiryError) as missing_both:
        resolve_form_expiry(
            enabled=True,
            when=CUSTOM_EXPIRY_OPTION,
            custom_local="2026-03-09T10:00",
            now=BASE_NOW,
        )
    assert missing_both.value.code == CREDENTIAL_EXPIRY_INVALID


# --- Contract and gateway result tests (C07) ----------------------------------


def test_collection_request_and_result_contract_validation() -> None:
    req = SecureCredentialCollectionRequest(
        service_name="Example",
        credential_key="example_key",
        purpose="Temporary access",
        expires_at=datetime(2026, 6, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    assert req.expires_at == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValidationError):
        SecureCredentialCollectionRequest(
            service_name="Example",
            credential_key="example_key",
            purpose="Naive rejected",
            expires_at=datetime(2026, 6, 1, 12, 0),
        )

    # C07: delete flow cannot carry expires_at.
    with pytest.raises(ValidationError):
        SecureCredentialCollectionRequest(
            service_name="Example",
            credential_key="example_key",
            purpose="Delete",
            flow="credential_deletion",
            expires_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        )

    res = SecureCredentialCollectionResult(
        credential_request_id="cred-1",
        status="credential_stored",
        credential_key="example_key",
        service_name="Example",
        expires_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert res.expires_at == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_protocol1_wire_models_accept_expires_at_on_collect_and_result_and_reject_on_delete() -> None:
    collect_body = wire_models.CredentialFormRequestBody.model_validate(
        {
            "form_id": "form_1",
            "task_id": "task_1",
            "root_task_id": "task_root_1",
            "continue_task": True,
            "user_approved": True,
            "flow": {
                "type": "collect",
                "service_name": "Example",
                "credential_key": "example.token",
                "purpose": "Temporary token",
                "store_target": "sealbox",
                "expires_at": "2026-11-26T18:00:00.000Z",
                "fields": [
                    {
                        "name": "token",
                        "required": True,
                        "kind": "token",
                        "visibility": "secret",
                        "value_source": "user_input",
                    }
                ],
            },
        }
    )
    assert collect_body.flow.expires_at == "2026-11-26T18:00:00.000Z"

    with pytest.raises(ValidationError):
        wire_models.CredentialFormRequestBody.model_validate(
            {
                "form_id": "form_1",
                "task_id": "task_1",
                "root_task_id": "task_root_1",
                "continue_task": False,
                "user_approved": True,
                "flow": {
                    "type": "delete",
                    "service_name": "Example",
                    "credential_key": "example.token",
                    "expires_at": "2026-11-26T18:00:00.000Z",
                },
            }
        )

    result_body = wire_models.CredentialFormResultBody.model_validate(
        {
            "form_id": "form_1",
            "task_id": "task_1",
            "status": "credential_stored",
            "continue_task": True,
            "credential_key": "example.token",
            "service_name": "Example",
            "flow": "collect",
            "credential": {
                "fields": [
                    {
                        "name": "token",
                        "kind": "token",
                        "visibility": "secret",
                        "value_source": "user_input",
                    }
                ],
                "expires_at": "2026-11-26T18:00:00.000Z",
            },
        }
    )
    assert result_body.credential is not None
    assert result_body.credential.expires_at == "2026-11-26T18:00:00.000Z"


@pytest.mark.asyncio
async def test_gateway_publish_result_uses_credential_expiry_not_session_expiry() -> None:
    published: list[SecureCredentialCollectionResult] = []

    class FakePublisher:
        async def publish(self, result: Any, **_kwargs: Any) -> SendOutcome:
            published.append(result)
            return SendOutcome.SENT

    class FakeSessions:
        def get_session(self, key: str) -> Any:
            return SimpleNamespace(session_key=key)

    workflow = GatewayCredentialWorkflow.__new__(GatewayCredentialWorkflow)
    workflow._publisher = FakePublisher()  # type: ignore[attr-defined]
    workflow._session_repository = FakeSessions()  # type: ignore[attr-defined]

    request = SecureCredentialCollectionRequest(
        credential_request_id="cred-req-1",
        service_name="Example",
        credential_key="example_key",
        purpose="Test",
    )
    session_expiry = "2026-11-26T06:15:00Z"
    credential_expiry = "2026-11-27T12:00:00Z"

    # Case 1: credential has its own expiry distinct from the 15-minute form session expiry.
    await workflow.publish_result(
        request_content=request,
        session_payload={
            "session_id": "sess-1",
            "status": "credential_stored",
            "expires_at": session_expiry,
            "result": {
                "status": "success",
                "content": {
                    "status": "credential_stored",
                    "metadata": {"credential_key": "example_key", "expires_at": credential_expiry},
                },
            },
        },
        correlation_id="corr-1",
        conversation_id="conv-1",
        session_key="sk-1",
    )
    assert published[-1].expires_at == datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    # Case 2: lasting credential (no metadata expires_at) must NOT inherit session_payload["expires_at"].
    await workflow.publish_result(
        request_content=request,
        session_payload={
            "session_id": "sess-2",
            "status": "credential_stored",
            "expires_at": session_expiry,
            "result": {
                "status": "success",
                "content": {
                    "status": "credential_stored",
                    "metadata": {"credential_key": "example_key"},
                },
            },
        },
        correlation_id="corr-2",
        conversation_id="conv-1",
        session_key="sk-1",
    )
    assert published[-1].expires_at is None


# --- Storage, Sealbox --ttl, overwrite, and C01/C04/C08 guards ----------------


def _sealbox_request(*, store_target: str = "sealbox", include_proposed_expiry: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "credential_key": "temp_service",
        "display_name": "Temporary Service",
        "purpose": "Short-lived test credential",
        "store_target": store_target,
        "fields": [
            {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
            {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
            {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            {"name": "auth_file", "kind": "credential_file", "visibility": "secret", "value_source": "user_input"},
        ],
    }
    if include_proposed_expiry is not None:
        payload["expires_at"] = include_proposed_expiry
    return payload


def test_sealbox_write_applies_ttl_to_every_record_and_overwrite_clears_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[:2] == ["password", "generate"]:
            return subprocess.CompletedProcess(command, 0, stdout='{"password": "generated-temp-pw"}', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    expiry = BASE_NOW + timedelta(seconds=90)
    result = collect_credential(
        _sealbox_request(),
        {
            "username": "alice",
            "password": "temp-password-value",
            "api_token": "temp-token-value",
            "auth_file": '{"access_token": "temp"}',
        },
        scope=OWNER_SCOPE,
        expires_at=expiry,
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    assert result.status == "success", result.error
    assert result.content["expires_at"] == "2026-03-07T12:01:30Z"
    assert result.content["metadata"]["expires_at"] == "2026-03-07T12:01:30Z"

    # All 3 Sealbox writes (credential set + token secret set + file secret set) must receive --ttl 90.
    assert len(commands) == 3
    for cmd in commands:
        ttl_index = cmd.index("--ttl")
        assert cmd[ttl_index + 1] == "90"

    saved = load_catalog(scope=OWNER_SCOPE).credentials["temp_service"]
    assert saved.expires_at == "2026-03-07T12:01:30Z"

    # Overwriting without expires_at via form/trusted storage clears expires_at and writes without --ttl (A1).
    commands.clear()
    overwrite = collect_credential(
        _sealbox_request(),
        {
            "username": "alice",
            "password": "lasting-password-value",
            "api_token": "lasting-token-value",
            "auth_file": '{"access_token": "lasting"}',
        },
        scope=OWNER_SCOPE,
        expires_at=None,
        allow_expiry_change=True,
    )
    assert overwrite.status == "success", overwrite.error
    assert "expires_at" not in overwrite.content
    assert "expires_at" not in overwrite.content["metadata"]
    assert len(commands) == 3
    for cmd in commands:
        assert "--ttl" not in cmd

    updated = load_catalog(scope=OWNER_SCOPE).credentials["temp_service"]
    assert updated.expires_at is None


def test_storage_accepts_ten_second_ttl_and_refuses_non_sealbox_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[:2] == ["password", "generate"]:
            return subprocess.CompletedProcess(command, 0, stdout='{"password": "generated-temp-pw"}', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    # C04: storage accepts 10s (no 5-minute minimum at storage layer).
    short_result = collect_credential(
        {
            "credential_key": "short_token",
            "display_name": "Short Token",
            "purpose": "Live test TTL",
            "store_target": "sealbox",
            "fields": [{"name": "token", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
        },
        {"token": "short-lived"},
        scope=OWNER_SCOPE,
        expires_at=BASE_NOW + timedelta(seconds=10),
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    assert short_result.status == "success", short_result.error
    assert commands == [["secret", "set", "team-nats-agents:default:short_token:token", "--output", "json", "--ttl", "10"]]

    # A1: non-Sealbox source refuses expiry with credential_expiry_unsupported_source and writes nothing.
    wrote = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal wrote
        wrote = True
        raise AssertionError("Non-Sealbox write should not have been called")

    monkeypatch.setattr(credential_catalog, "_write_bitwarden_login_item", fail_if_called)
    non_sealbox = collect_credential(
        _sealbox_request(store_target="bitwarden"),
        {"username": "alice", "password": "pw", "api_token": "tok", "auth_file": "file"},
        scope=OWNER_SCOPE,
        expires_at=BASE_NOW + timedelta(hours=1),
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    assert non_sealbox.status == "error"
    assert non_sealbox.content["error_code"] == CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE
    assert wrote is False


def test_c01_agent_actions_cannot_set_expiry_and_request_payload_expiry_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[:2] == ["password", "generate"]:
            return subprocess.CompletedProcess(command, 0, stdout='{"password": "generated-temp-pw"}', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    proposed = "2026-03-07T12:10:00Z"

    # C01(a): agent calling credential.collect with expires_at is refused and writes nothing.
    action_result = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "existing_key",
                    "display_name": "Existing",
                    "purpose": "Attempted agent expiry",
                    "store_target": "sealbox",
                    "expires_at": proposed,
                    "fields": [
                        {
                            "name": "password",
                            "kind": "password",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                        }
                    ],
                }
            },
        ),
        TOOL_CONTEXT,
    )
    assert action_result.status == "error"
    assert action_result.content["error_code"] == CREDENTIAL_EXPIRY_FORM_ONLY
    assert commands == []
    assert "existing_key" not in load_catalog(scope=OWNER_SCOPE).credentials

    # C01(b): register_existing_source refuses expires_at even for a Sealbox source.
    reg_result = register_existing_source(
        {
            "credential_key": "existing_sealbox",
            "field_name": "token",
            "source": "sealbox",
            "item_id": "team-nats-agents:default:existing_sealbox:token",
            "field_ref": "value",
            "expires_at": proposed,
        },
        scope=OWNER_SCOPE,
    )
    assert reg_result.status == "error"
    assert reg_result.content["error_code"] == CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE

    # C01(c): collect_credential ignores request_payload["expires_at"] when kwarg expires_at is None.
    unticked = collect_credential(
        _sealbox_request(include_proposed_expiry=proposed),
        {"username": "alice", "password": "pw", "api_token": "tok", "auth_file": "file"},
        scope=OWNER_SCOPE,
        expires_at=None,
    )
    assert unticked.status == "success", unticked.error
    assert "expires_at" not in unticked.content
    assert load_catalog(scope=OWNER_SCOPE).credentials["temp_service"].expires_at is None
    assert all("--ttl" not in cmd for cmd in commands)


def test_catalog_credential_validates_expires_at() -> None:
    cred = CatalogCredential(
        display_name="Temp",
        purpose="Test",
        expires_at="2026-06-01T14:00:00+02:00",
    )
    assert cred.expires_at == "2026-06-01T12:00:00Z"

    with pytest.raises(ValidationError):
        CatalogCredential(display_name="Bad", purpose="Test", expires_at="2026-06-01T12:00:00")

    with pytest.raises(ValidationError):
        CatalogCredential(display_name="Bad", purpose="Test", expires_at="not-a-date")


# --- C10 / Lead Decision (1): agent actions never clear or replace an expiry --


def test_c10_agent_collect_over_temporary_key_keeps_expiry_and_remaining_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[:2] == ["password", "generate"]:
            return subprocess.CompletedProcess(command, 0, stdout='{"password": "generated-temp-pw"}', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    # Person stores a temporary credential via the form (allow_expiry_change=True).
    now_start = datetime.now(timezone.utc).replace(microsecond=0)
    expiry = now_start + timedelta(minutes=30)
    saved = collect_credential(
        _sealbox_request(),
        {"username": "alice", "password": "p1", "api_token": "t1", "auth_file": "f1"},
        scope=OWNER_SCOPE,
        expires_at=expiry,
        allow_expiry_change=True,
        now=now_start,
    )
    assert saved.status == "success", saved.error
    expected_iso = format_utc_timestamp(expiry)
    assert saved.content["expires_at"] == expected_iso

    # Agent runs credential.collect with store_generated_secret and no expires_at:
    # must preserve the person's expires_at and pass remaining --ttl on every Sealbox write.
    commands.clear()
    agent_write = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "temp_service",
                    "display_name": "Temporary Service",
                    "purpose": "Agent rotated generated secret",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "password",
                            "kind": "password",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                        }
                    ],
                }
            },
        ),
        TOOL_CONTEXT,
    )
    assert agent_write.status == "success", agent_write.error
    assert agent_write.content["expires_at"] == expected_iso
    assert agent_write.content["metadata"]["expires_at"] == expected_iso
    assert load_catalog(scope=OWNER_SCOPE).credentials["temp_service"].expires_at == expected_iso
    write_commands = [cmd for cmd in commands if cmd[:2] in (["credential", "set"], ["secret", "set"])]
    assert write_commands, "expected Sealbox write command"
    for cmd in write_commands:
        ttl_index = cmd.index("--ttl")
        ttl_val = int(cmd[ttl_index + 1])
        assert 1 <= ttl_val <= 1800

    # Agent collect over a temporary key with a non-Sealbox target is refused and writes nothing.
    commands.clear()
    non_sealbox = collect_credential(
        _sealbox_request(store_target="bitwarden"),
        {"username": "alice", "password": "p2", "api_token": "t2", "auth_file": "f2"},
        scope=OWNER_SCOPE,
        allow_expiry_change=False,
        now=now_start + timedelta(minutes=5),
    )
    assert non_sealbox.status == "error"
    assert non_sealbox.content["error_code"] == CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE
    assert commands == []
    assert load_catalog(scope=OWNER_SCOPE).credentials["temp_service"].expires_at == expected_iso

    # Agent collect after expires_at has passed is refused with CREDENTIAL_EXPIRED and writes nothing.
    commands.clear()
    expired_overwrite = collect_credential(
        {
            "credential_key": "temp_service",
            "display_name": "Temporary Service",
            "purpose": "Late overwrite",
            "store_target": "sealbox",
            "fields": [
                {
                    "name": "password",
                    "kind": "password",
                    "visibility": "secret",
                    "value_source": "store_generated_secret",
                }
            ],
        },
        {},
        scope=OWNER_SCOPE,
        allow_expiry_change=False,
        now=expiry + timedelta(seconds=1),
    )
    assert expired_overwrite.status == "error"
    assert expired_overwrite.content["error_code"] == CREDENTIAL_EXPIRED
    assert commands == []
    assert load_catalog(scope=OWNER_SCOPE).credentials["temp_service"].expires_at == expected_iso


def test_c10_register_existing_source_refuses_temporary_key_active_or_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_sealbox(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)
    monkeypatch.setattr(credential_catalog, "_resolve_source_ref", lambda *_a, **_k: "secret-val")

    # Active temporary key: refused with CREDENTIAL_EXPIRY_FORM_ONLY and catalog unchanged.
    active_expiry = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    saved = collect_credential(
        _sealbox_request(),
        {"username": "alice", "password": "p1", "api_token": "t1", "auth_file": "f1"},
        scope=OWNER_SCOPE,
        expires_at=active_expiry,
        allow_expiry_change=True,
    )
    assert saved.status == "success"
    expected_iso = format_utc_timestamp(active_expiry)

    refused_active = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "temp_service",
                "field_name": "api_token",
                "source": "sealbox",
                "item_id": "team-nats-agents:default:temp_service:api_token",
                "field_ref": "value",
            },
        ),
        TOOL_CONTEXT,
    )
    assert refused_active.status == "error"
    assert refused_active.content["error_code"] == CREDENTIAL_EXPIRY_FORM_ONLY
    assert load_catalog(scope=OWNER_SCOPE).credentials["temp_service"].expires_at == expected_iso

    # Already-expired temporary key (not yet removed by janitor): also refused.
    past_expiry = BASE_NOW - timedelta(hours=1)
    past_cred = CatalogCredential(
        display_name="Past Temp",
        purpose="Expired",
        expires_at=format_utc_timestamp(past_expiry),
    )
    credential_catalog._save_catalog_credential("expired_temp", past_cred, scope=OWNER_SCOPE)
    refused_expired = register_existing_source(
        {
            "credential_key": "expired_temp",
            "field_name": "api_token",
            "source": "sealbox",
            "item_id": "team-nats-agents:default:expired_temp:api_token",
            "field_ref": "value",
        },
        scope=OWNER_SCOPE,
    )
    assert refused_expired.status == "error"
    assert refused_expired.content["error_code"] == CREDENTIAL_EXPIRY_FORM_ONLY
    assert load_catalog(scope=OWNER_SCOPE).credentials["expired_temp"].expires_at == format_utc_timestamp(past_expiry)


# --- C13: OverflowError handling and whole-second timestamps ------------------


def test_c13_overflow_dates_raise_expiry_error_and_stored_times_drop_microseconds() -> None:
    with pytest.raises(CredentialExpiryError) as high_exc:
        resolve_form_expiry(
            enabled=True,
            when=CUSTOM_EXPIRY_OPTION,
            custom_local="9999-12-31T23:59",
            tz_offset_minutes_behind_utc="720",
            now=BASE_NOW,
        )
    assert high_exc.value.code == CREDENTIAL_EXPIRY_INVALID

    with pytest.raises(CredentialExpiryError) as low_exc:
        resolve_form_expiry(
            enabled=True,
            when=CUSTOM_EXPIRY_OPTION,
            custom_local="0001-01-01T00:00",
            tz_offset_minutes_behind_utc="-840",
            now=BASE_NOW,
        )
    assert low_exc.value.code == CREDENTIAL_EXPIRY_INVALID

    with pytest.raises(CredentialExpiryError) as zone_exc:
        resolve_form_expiry(
            enabled=True,
            when=CUSTOM_EXPIRY_OPTION,
            custom_local="9999-12-31T23:59",
            timezone_name="America/New_York",
            now=BASE_NOW,
        )
    assert zone_exc.value.code == CREDENTIAL_EXPIRY_INVALID

    now_with_micros = datetime(2026, 11, 26, 6, 9, 21, 92643, tzinfo=timezone.utc)
    preset = resolve_form_expiry(enabled=True, when="15m", now=now_with_micros)
    assert preset is not None
    assert preset.microsecond == 0
    assert format_utc_timestamp(preset) == "2026-11-26T06:24:21Z"
    assert remaining_ttl_seconds(preset, now=now_with_micros) == 900
    assert format_utc_timestamp(now_with_micros) == "2026-11-26T06:09:21Z"
