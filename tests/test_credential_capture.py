"""Tests for credential.capture_file (B1-B5, C01-C09, and folder access)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal, agent_role_resolver, agent_type_id
from access_control.identity import ContextVerifier
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    GrantKind,
    GrantV1,
    LinkPurpose,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker import credential_capture, credential_catalog
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.credential_capture import (
    CAPTURE_ACCESS_GATE_REQUIRED,
    CAPTURE_APPROVAL_SOURCE,
    CAPTURE_EXTERNAL_SOURCE_KEY_REFUSED,
    CAPTURE_FIELD_NAME_MISMATCH,
    CAPTURE_FILE_EMPTY,
    CAPTURE_FILE_INVALID_ENCODING,
    CAPTURE_FILE_NOT_FOUND,
    CAPTURE_FILE_TOO_LARGE,
    CAPTURE_FOLDER_ACCESS_DENIED,
    CAPTURE_FOLDER_CHECK_UNAVAILABLE,
    CAPTURE_INVALID_PARAMS,
    CAPTURE_NOT_REGULAR_FILE,
    CAPTURE_PATH_OUTSIDE_SCOPE,
    CAPTURE_PERSON_CREDENTIAL_REFUSED,
    CAPTURE_PERSON_REQUIRED,
    CAPTURE_SECRET_PARAM_REJECTED,
    CAPTURE_SYMLINK_REFUSED,
    CAPTURE_TEAM_KEY_REFUSED,
    CAPTURE_TEAM_SCOPE_REFUSED,
    CAPTURE_UNEXPECTED_PARAMS,
    NOTICE_STATUS_FAILED,
    NOTICE_STATUS_OFF,
    NOTICE_STATUS_SENT,
    SOURCE_FILE_STATUS_ALREADY_GONE,
    SOURCE_FILE_STATUS_CHANGED_AFTER_READ,
    SOURCE_FILE_STATUS_DELETED,
    capture_file_credential,
    is_agent_captured_for_person,
)
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialOwnerScope,
    collect_credential,
    get_credential_metadata,
    linked_credential_expired,
    load_catalog,
    register_existing_source,
    resolve_secret_field,
)
from secure_tool_broker.credential_expiry import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_EXPIRY_INVALID,
    CREDENTIAL_EXPIRY_REQUIRED,
    CREDENTIAL_EXPIRY_SHARED_RECORD,
    CREDENTIAL_EXPIRY_TOO_FAR,
    CREDENTIAL_EXPIRY_TOO_SOON,
    CREDENTIAL_REPLACEMENT_FORM_ONLY,
    format_expiry_label,
    format_utc_timestamp,
)
from secure_tool_broker.credential_transfer import (
    export_credentials,
    import_credentials,
)
from secure_tool_broker.defaults import ENV_CREDENTIAL_MAX_FILE_BYTES, ENV_NOTIFY_ON_AGENT_CAPTURE
from secure_tool_broker.janitor import clean_expired_credentials
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.providers import ToolContext, ToolResult
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_AGENT_ID,
    TEST_AGENT_TYPE,
    TEST_TEAM,
    access_store,
    agent_context,
    caller_headers,
    context_verify_keys,
    linked_gate,
    signed_task_context,
    store_account_link,
)
from tests.test_access_service import BINARY as CEDAR_BINARY

NEEDS_CEDAR = pytest.mark.skipif(
    not CEDAR_BINARY.is_file(),
    reason="build ua-cedar-authorizer first (cargo build)",
)
PERSON_ID = "alice"
OTHER_PERSON_ID = "bob"
BASE_NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeSealbox:
    """In-memory Sealbox CLI double recording --ttl, stdin values, and deletes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.secrets: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.commands: list[list[str]] = []
        self.fail_next_set: bool = False
        self.on_set_hook: Any = None

    def run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        cmd = list(command)
        with self.lock:
            self.commands.append(cmd)
        if cmd[:2] in (["secret", "set"], ["credential", "set"]):
            key = cmd[2]
            if self.on_set_hook is not None:
                self.on_set_hook(cmd, key, input_text)
            with self.lock:
                if self.fail_next_set:
                    self.fail_next_set = False
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sealbox backend unavailable")
                ttl: int | None = None
                if "--ttl" in cmd:
                    ttl = int(cmd[cmd.index("--ttl") + 1])
                self.secrets[key] = str(input_text or "")
                self.ttls[key] = ttl
            return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")
        if cmd[:2] == ["secret", "get"]:
            key = cmd[2]
            with self.lock:
                if key not in self.secrets:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="secret not found")
                value = self.secrets[key]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"value": value}), stderr="")
        if cmd[:2] == ["password", "generate"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"password": "generated-sealbox-password-123"}', stderr=""
            )
        if cmd[:2] in (["secret", "delete"], ["credential", "delete"]):
            key = cmd[2]
            with self.lock:
                existed = key in self.secrets
                self.secrets.pop(key, None)
                self.ttls.pop(key, None)
            if not existed:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="secret not found")
            return subprocess.CompletedProcess(cmd, 0, stdout='{"deleted": true}', stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")


@pytest.fixture
def env_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    for person in (PERSON_ID, OTHER_PERSON_ID):
        (layout.person_dir(person) / "scratch").mkdir(parents=True, exist_ok=True)
    (layout.team_dir / "finance").mkdir(parents=True, exist_ok=True)
    (layout.root / "team.env").write_text("TOP_SECRET=do-not-read\n", encoding="utf-8")

    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.delenv(ENV_NOTIFY_ON_AGENT_CAPTURE, raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL_MAX_FILE_BYTES, raising=False)

    sealbox = FakeSealbox()
    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", lambda **_kwargs: {})
    monkeypatch.setattr(credential_catalog, "run_sealbox", sealbox.run)
    # Also resolve through fake sealbox (and real env vars) when reading via resolve_credential / transfer export.
    real_resolve_source_ref = credential_catalog._resolve_source_ref
    monkeypatch.setattr(
        credential_catalog,
        "_resolve_source_ref",
        lambda ref, **kwargs: (
            sealbox.secrets.get(ref.item_id or "")
            if ref.source == "sealbox"
            else (real_resolve_source_ref(ref, **kwargs) if ref.source == "env" else None)
        ),
    )
    credential_catalog._SECRET_CACHE.clear()

    store = access_store(tmp_path / "access.db")
    gate = linked_gate(store, team_id=TEST_TEAM, instance_root=layout.root)
    return layout, sealbox, store, gate


def _seed_registered_sealbox_row(
    scope: CredentialOwnerScope, credential_key: str, field_name: str, record_key: str
) -> CatalogCredential:
    """Write a catalog row registered against a Sealbox record, as agent registration allowed before C28.

    Agent ``credential.register_existing_source`` now refuses Sealbox sources,
    so tests that need such a row (old rows, shared-record collisions) write it
    directly.
    """

    row = CatalogCredential(
        display_name=credential_key,
        purpose="Registered before Sealbox registration was refused",
        approval={"status": "registered_by_agent", "source": "existing_source_registration"},
        fields={
            field_name: CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="existing_source",
                resolution_order=[
                    CatalogSourceRef(source="sealbox", item_id=record_key, item_name=record_key, field_ref="value")
                ],
            )
        },
    )
    credential_catalog._save_catalog_credential(credential_key, row, scope=scope)
    return row


def _person_context(
    *,
    person_id: str = PERSON_ID,
    agent_type: str = TEST_AGENT_TYPE,
    agent_id: str = TEST_AGENT_ID,
    team_id: str = TEST_TEAM,
    invocation_id: str | None = None,
    roles: tuple[str, ...] = ("member",),
    work_id: str = "ctx-1",
) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        named_agent_type=agent_type,
        canonical_user_id=person_id,
        principal_type="user",
        team_id=team_id,
        invocation_id=invocation_id,
        person_roles=roles,
        work_id=work_id,
    )


def _valid_params(
    *,
    path: str = "scratch/token.txt",
    credential_key: str = "temp_api_token",
    display_name: str = "Temporary API Token",
    purpose: str = "Short-lived API sign-in",
    field_name: str | None = None,
    expires_at: str | None = None,
    now: datetime = BASE_NOW,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": path,
        "credential_key": credential_key,
        "display_name": display_name,
        "purpose": purpose,
        "expires_at": expires_at if expires_at is not None else format_utc_timestamp(now + timedelta(hours=1)),
    }
    if field_name is not None:
        payload["field_name"] = field_name
    return payload


def _auth_gate(layout: InstanceLayout, store: Any) -> BrokerAccessGate:
    catalogs = CatalogSource(layout.root)
    access = AccessService(
        store=store,
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id=TEST_TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
        lead_agent_type="TeamArchitect",
    )
    return BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=access,
        links=store,
    )


def _person_notices(store: Any, person_id: str = PERSON_ID) -> list[Any]:
    return [notice for notice, _read in store.notices_for(TEST_TEAM, person_id=person_id, roles=("member",))]


# --- B1, B4: Happy path, multi-line file, no secret leakage, notices ----------


@pytest.mark.asyncio
async def test_http_capture_file_stores_with_ttl_deletes_source_sends_one_notice_and_leaks_no_secret(
    env_setup,
    caplog: pytest.LogCaptureFixture,
) -> None:
    layout, sealbox, store, gate = env_setup
    secret_text = '{\n  "access_token": "super-secret-value-9911",\n  "refresh_token": "refresh-8822"\n}'
    source = layout.person_dir(PERSON_ID) / "scratch" / "auth.json"
    source.write_text(f"\n{secret_text}\n", encoding="utf-8")

    expiry = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    expiry_iso = format_utc_timestamp(expiry)

    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    with caplog.at_level(logging.DEBUG):
        async with TestClient(TestServer(service.build_app())) as client:
            response = await client.post(
                "/v1/tools/credential_catalog",
                headers=caller_headers(),
                json={
                    "provider": "credential_catalog",
                    "action": "credential.capture_file",
                    "params": {
                        "path": "scratch/auth.json",
                        "credential_key": "cli_auth_json",
                        "display_name": "CLI Auth JSON",
                        "purpose": "Temporary sign-in auth file",
                        "field_name": "auth_json",
                        "expires_at": expiry_iso,
                    },
                    "context": agent_context(PERSON_ID, named_agent_type="ResearchAgent"),
                },
            )
            body = await response.json()

    assert response.status == 200, body
    assert body["status"] == "success", body
    content = body["content"]
    assert content["status"] == "credential_captured"
    assert content["credential_key"] == "cli_auth_json"
    assert content["field_name"] == "auth_json"
    assert content["expires_at"] == expiry_iso
    assert content["source_file_deleted"] is True
    assert content["notice_sent"] is True
    assert content["notice_status"] == NOTICE_STATUS_SENT
    assert not source.exists()

    # Secret stored in person scope with TTL and exact stripped multi-line content.
    person_catalog = load_catalog(scope=CredentialOwnerScope.user(PERSON_ID))
    saved = person_catalog.credentials["cli_auth_json"]
    assert saved.expires_at == expiry_iso
    assert is_agent_captured_for_person(saved, PERSON_ID)
    assert saved.approval["captured_by_agent_type"] == "ResearchAgent"
    assert saved.approval["captured_for_person_id"] == PERSON_ID
    assert "cli_auth_json" not in load_catalog(scope=CredentialOwnerScope.team(TEST_TEAM)).credentials

    record_key = saved.fields["auth_json"].resolution_order[0].item_id
    assert record_key is not None
    assert sealbox.secrets[record_key] == secret_text
    assert sealbox.ttls[record_key] is not None and 7000 <= sealbox.ttls[record_key] <= 7205

    # Exactly one notice naming agent type, display name, and deletion time, with no secret.
    notices = _person_notices(store)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.kind is AccessNoticeKind.CREDENTIAL_CAPTURED
    assert notice.recipient_person_id == PERSON_ID
    assert "ResearchAgent" in notice.title
    assert "CLI Auth JSON" in notice.title
    assert format_expiry_label(expiry) in notice.summary

    # Secret never appears in HTTP response, notice, or logs.
    serialized = json.dumps(body) + notice.title + notice.summary + caplog.text
    assert "super-secret-value-9911" not in serialized
    assert "refresh-8822" not in serialized


def test_capture_default_field_name_recapture_clears_cache_and_sends_distinct_notices(
    env_setup,
) -> None:
    layout, sealbox, store, gate = env_setup
    first_file = layout.person_dir(PERSON_ID) / "scratch" / "one.txt"
    first_file.write_text("first-secret-value\n", encoding="utf-8")

    # Even with identical invocation_id, two captures send two notices (C03).
    ctx = _person_context(invocation_id="same-inv-id")
    first = capture_file_credential(
        _valid_params(path="scratch/one.txt", credential_key="re_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert first.status == "success", first.error
    assert first.content["field_name"] == "secret"
    assert not first_file.exists()

    # Populate secret cache by resolving the credential.
    resolved_one = resolve_secret_field("re_key", "secret", scope=CredentialOwnerScope.user(PERSON_ID), now=BASE_NOW)
    assert not isinstance(resolved_one, credential_catalog.ToolResult)
    assert resolved_one.value == "first-secret-value"

    # Re-capture with a new value and new expiry clears _SECRET_CACHE immediately (C01).
    second_file = layout.person_dir(PERSON_ID) / "scratch" / "two.txt"
    second_file.write_text("second-secret-value\n", encoding="utf-8")
    later_expiry = format_utc_timestamp(BASE_NOW + timedelta(hours=3))
    second = capture_file_credential(
        _valid_params(path="scratch/two.txt", credential_key="re_key", expires_at=later_expiry, now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert second.status == "success", second.error
    assert not second_file.exists()

    resolved_two = resolve_secret_field("re_key", "secret", scope=CredentialOwnerScope.user(PERSON_ID), now=BASE_NOW)
    assert not isinstance(resolved_two, credential_catalog.ToolResult)
    assert resolved_two.value == "second-secret-value"
    assert load_catalog(scope=CredentialOwnerScope.user(PERSON_ID)).credentials["re_key"].expires_at == later_expiry

    # Two captures produced two notices.
    notices = _person_notices(store)
    assert len(notices) == 2
    assert len({n.notice_id for n in notices}) == 2

    # Even an already-expired agent-captured key can be replaced by capture_file.
    third_file = layout.person_dir(PERSON_ID) / "scratch" / "three.txt"
    third_file.write_text("third-secret-value\n", encoding="utf-8")
    after_expiry = BASE_NOW + timedelta(hours=5)
    third = capture_file_credential(
        _valid_params(path="scratch/three.txt", credential_key="re_key", now=after_expiry),
        context=ctx,
        access_gate=gate,
        now=after_expiry,
    )
    assert third.status == "success", third.error
    resolved_three = resolve_secret_field("re_key", "secret", scope=CredentialOwnerScope.user(PERSON_ID), now=after_expiry)
    assert not isinstance(resolved_three, credential_catalog.ToolResult)
    assert resolved_three.value == "third-secret-value"


def test_capture_notice_suppressed_by_env_setting(env_setup, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, _sealbox, store, gate = env_setup
    monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, "false")
    source = layout.person_dir(PERSON_ID) / "scratch" / "quiet.txt"
    source.write_text("quiet-secret", encoding="utf-8")

    result = capture_file_credential(
        _valid_params(path="scratch/quiet.txt", credential_key="quiet_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert result.status == "success", result.error
    assert result.content["notice_sent"] is False
    assert _person_notices(store) == []


# --- B2: All refusals leave the source file in place --------------------------


@pytest.mark.asyncio
async def test_no_person_refused_over_http_and_direct_call(env_setup) -> None:
    layout, _sealbox, _store, gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "sys.txt"
    source.write_text("stay-put", encoding="utf-8")

    # Direct call guard (system work / no person).
    direct = capture_file_credential(
        _valid_params(path="scratch/sys.txt", now=BASE_NOW),
        context=ToolContext(agent_id="sys-1", named_agent_type="MemoryReviewAgent", principal_type="system"),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert direct.status == "error"
    assert direct.content["error_code"] == CAPTURE_PERSON_REQUIRED
    assert direct.content["source_file_deleted"] is False
    assert source.exists()

    # Real HTTP call with system context: rejected at broker service before executor (C04).
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path="scratch/sys.txt", now=BASE_NOW),
                "context": {
                    "principal_type": "system",
                    "team_id": TEST_TEAM,
                    "agent_id": "sys-1",
                    "named_agent_type": "MemoryReviewAgent",
                    "task_context": signed_task_context(None),
                },
            },
        )
        text = await response.text()

    assert response.status == 403
    assert "trusted registered user context" in text.lower()
    assert source.exists()


def test_param_allowlist_secret_params_team_scope_and_metadata_bounds(env_setup) -> None:
    layout, _sealbox, _store, gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "param.txt"
    source.write_text("keep-me", encoding="utf-8")
    ctx = _person_context()

    # Secret-bearing parameter refused without echoing value.
    secret_attempt = _valid_params(path="scratch/param.txt", now=BASE_NOW)
    secret_attempt["value"] = "leaked-param-secret-42"
    res = capture_file_credential(secret_attempt, context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_SECRET_PARAM_REJECTED
    assert "leaked-param-secret-42" not in (res.error or "") + json.dumps(dict(res.content))
    assert source.exists()

    # Team scope override refused.
    team_attempt = _valid_params(path="scratch/param.txt", now=BASE_NOW)
    team_attempt["owner_scope_kind"] = "team"
    res = capture_file_credential(team_attempt, context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_TEAM_SCOPE_REFUSED
    assert source.exists()

    # Unexpected parameter refused.
    extra_attempt = _valid_params(path="scratch/param.txt", now=BASE_NOW)
    extra_attempt["unknown_flag"] = True
    res = capture_file_credential(extra_attempt, context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_UNEXPECTED_PARAMS
    assert source.exists()

    # field_name containing ':' refused (C05), oversized or multi-line display_name refused (C03).
    for bad_override in (
        {"field_name": "prod:token"},
        {"credential_key": "bad:key"},
        {"display_name": "x" * 121},
        {"display_name": "line1\nline2"},
        {"purpose": "p" * 401},
        {"purpose": "bad\x00purpose"},
    ):
        attempt = _valid_params(path="scratch/param.txt", now=BASE_NOW)
        attempt.update(bad_override)
        res = capture_file_credential(attempt, context=ctx, access_gate=gate, now=BASE_NOW)
        assert res.status == "error", bad_override
        assert res.content["error_code"] == CAPTURE_INVALID_PARAMS
        assert res.content["source_file_deleted"] is False
        assert source.exists()


def test_file_safety_refusals_outside_scope_symlink_nonregular_empty_oversized_and_non_utf8(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _sealbox, _store, gate = env_setup
    ctx = _person_context()
    alice_scratch = layout.person_dir(PERSON_ID) / "scratch"

    # Outside person's folders (team.env, other person's folder, traversal).
    bob_file = layout.person_dir(OTHER_PERSON_ID) / "scratch" / "bob.txt"
    bob_file.write_text("bobs-secret", encoding="utf-8")
    for outside in (str(layout.root / "team.env"), "../../../team.env", str(bob_file)):
        res = capture_file_credential(_valid_params(path=outside, now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
        assert res.status == "error"
        assert res.content["error_code"] == CAPTURE_PATH_OUTSIDE_SCOPE
    assert bob_file.exists()

    # Symlink file and symlink parent folder.
    real_target = alice_scratch / "real.txt"
    real_target.write_text("real-secret", encoding="utf-8")
    link_file = alice_scratch / "link.txt"
    os.symlink(real_target, link_file)
    res = capture_file_credential(_valid_params(path="scratch/link.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_SYMLINK_REFUSED
    assert link_file.is_symlink() and real_target.exists()

    link_dir = alice_scratch / "linkdir"
    os.symlink(layout.team_dir, link_dir)
    (layout.team_dir / "t.txt").write_text("team-text", encoding="utf-8")
    res = capture_file_credential(_valid_params(path="scratch/linkdir/t.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_SYMLINK_REFUSED
    assert (layout.team_dir / "t.txt").exists()

    # Non-regular files: directory, FIFO, and hard-linked file.
    res = capture_file_credential(_valid_params(path="scratch", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_NOT_REGULAR_FILE

    fifo_path = alice_scratch / "pipe.fifo"
    os.mkfifo(fifo_path)
    res = capture_file_credential(_valid_params(path="scratch/pipe.fifo", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_NOT_REGULAR_FILE
    assert fifo_path.exists()

    hard_link = alice_scratch / "hardlink.txt"
    os.link(real_target, hard_link)
    res = capture_file_credential(_valid_params(path="scratch/hardlink.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_NOT_REGULAR_FILE
    assert hard_link.exists() and real_target.exists()

    # Missing file.
    res = capture_file_credential(_valid_params(path="scratch/missing.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_FILE_NOT_FOUND

    # Empty and whitespace-only files.
    for name, content in (("empty.txt", ""), ("spaces.txt", "   \n\t  ")):
        f = alice_scratch / name
        f.write_text(content, encoding="utf-8")
        res = capture_file_credential(_valid_params(path=f"scratch/{name}", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
        assert res.status == "error"
        assert res.content["error_code"] == CAPTURE_FILE_EMPTY
        assert f.exists()

    # Oversized file (default 256 KiB and shared operator override from C06).
    big_file = alice_scratch / "big.txt"
    big_file.write_bytes(b"a" * (262_144 + 1))
    res = capture_file_credential(_valid_params(path="scratch/big.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_FILE_TOO_LARGE
    assert big_file.exists()

    # Raising SECURE_TOOL_BROKER_CREDENTIAL_COLLECTION_MAX_FILE_BYTES applies to capture too (C06).
    monkeypatch.setenv(ENV_CREDENTIAL_MAX_FILE_BYTES, str(512 * 1024))
    ok_big = capture_file_credential(_valid_params(path="scratch/big.txt", credential_key="big_ok", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert ok_big.status == "success", ok_big.error
    assert not big_file.exists()

    # Non-UTF-8 file: fixed error message leaks ZERO bytes of the file (C04).
    bad_utf8 = alice_scratch / "binary.bin"
    bad_utf8.write_bytes(b"\x80\xfe\xffSENSITIVE_MARKER_XYZ")
    res = capture_file_credential(_valid_params(path="scratch/binary.bin", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_FILE_INVALID_ENCODING
    combined = (res.error or "") + json.dumps(dict(res.content))
    assert "0x80" not in combined and "0xfe" not in combined and "SENSITIVE_MARKER_XYZ" not in combined
    assert bad_utf8.exists()

    # NUL byte refused.
    nul_file = alice_scratch / "nul.txt"
    nul_file.write_bytes(b"abc\x00def")
    res = capture_file_credential(_valid_params(path="scratch/nul.txt", now=BASE_NOW), context=ctx, access_gate=gate, now=BASE_NOW)
    assert res.status == "error"
    assert res.content["error_code"] == CAPTURE_FILE_INVALID_ENCODING
    assert nul_file.exists()


def test_expiry_refusals_report_broker_clock_and_window_and_leave_file(env_setup) -> None:
    layout, _sealbox, _store, gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "exp.txt"
    source.write_text("clock-secret", encoding="utf-8")
    ctx = _person_context()

    cases = [
        (None, CREDENTIAL_EXPIRY_REQUIRED),
        ("2026-11-26T13:00:00", CREDENTIAL_EXPIRY_INVALID),
        (format_utc_timestamp(BASE_NOW + timedelta(seconds=299)), CREDENTIAL_EXPIRY_TOO_SOON),
        (format_utc_timestamp(BASE_NOW + timedelta(days=90, seconds=1)), CREDENTIAL_EXPIRY_TOO_FAR),
    ]
    for raw_expiry, expected_code in cases:
        params = _valid_params(path="scratch/exp.txt", now=BASE_NOW)
        if raw_expiry is None:
            params.pop("expires_at")
        else:
            params["expires_at"] = raw_expiry
        res = capture_file_credential(params, context=ctx, access_gate=gate, now=BASE_NOW)
        assert res.status == "error"
        assert res.content["error_code"] == expected_code
        assert res.content["source_file_deleted"] is False
        # C08: broker UTC time and accepted window returned on every expiry refusal.
        assert res.content["broker_now_utc"] == "2026-11-26T12:00:00Z"
        assert res.content["earliest_expires_at"] == "2026-11-26T12:05:00Z"
        assert res.content["latest_expires_at"] == "2027-02-24T12:00:00Z"
        if expected_code != CREDENTIAL_EXPIRY_INVALID:
            assert "2026-11-26T12:00:00Z" in (res.error or "")
            assert "2026-11-26T12:05:00Z" in (res.error or "")
            assert "2027-02-24T12:00:00Z" in (res.error or "")
        assert source.exists()


def test_overwrite_guards_refuse_person_form_external_team_field_mismatch_and_shared_record(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, sealbox, _store, gate = env_setup
    ctx = _person_context()
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    team_scope = CredentialOwnerScope.team(TEST_TEAM)

    def write_src(name: str, text: str) -> Path:
        p = layout.person_dir(PERSON_ID) / "scratch" / name
        p.write_text(text, encoding="utf-8")
        return p

    # 1. Person-entered credential via secure form cannot be replaced by agent.
    form_save = collect_credential(
        {
            "credential_key": "person_key",
            "display_name": "Person Key",
            "purpose": "Entered via form",
            "store_target": "sealbox",
            "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "person-original-secret"},
        scope=alice_scope,
        expires_at=BASE_NOW + timedelta(hours=2),
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    assert form_save.status == "success", form_save.error
    person_record_key = load_catalog(scope=alice_scope).credentials["person_key"].fields["secret"].resolution_order[0].item_id
    assert person_record_key is not None

    src1 = write_src("p1.txt", "agent-hijack-attempt")
    refused_person = capture_file_credential(
        _valid_params(path="scratch/p1.txt", credential_key="person_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_person.status == "error"
    assert refused_person.content["error_code"] == CAPTURE_PERSON_CREDENTIAL_REFUSED
    assert sealbox.secrets[person_record_key] == "person-original-secret"
    assert src1.exists()

    # 2. Agent-captured key later replaced by person through form becomes person-owned.
    src_cap = write_src("cap.txt", "initial-agent-secret")
    cap_ok = capture_file_credential(
        _valid_params(path="scratch/cap.txt", credential_key="promoted_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert cap_ok.status == "success", cap_ok.error
    assert not src_cap.exists()
    person_takeover = collect_credential(
        {
            "credential_key": "promoted_key",
            "display_name": "Promoted",
            "purpose": "Person took over",
            "store_target": "sealbox",
            "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "person-owned-now"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert person_takeover.status == "success"
    src_retry = write_src("cap2.txt", "agent-cannot-overwrite-now")
    refused_after_takeover = capture_file_credential(
        _valid_params(path="scratch/cap2.txt", credential_key="promoted_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_after_takeover.status == "error"
    assert refused_after_takeover.content["error_code"] == CAPTURE_PERSON_CREDENTIAL_REFUSED
    assert src_retry.exists()

    # 3. External (non-Sealbox) source key refused.
    ext_cred = CatalogCredential(
        display_name="Bitwarden Key",
        purpose="External",
        approval={"status": "captured", "source": CAPTURE_APPROVAL_SOURCE, "captured_for_person_id": PERSON_ID},
        fields={
            "secret": CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="existing_source",
                resolution_order=[CatalogSourceRef(source="bitwarden", item_id="bw-1", field_ref="password")],
            )
        },
    )
    credential_catalog._save_catalog_credential("ext_key", ext_cred, scope=alice_scope)
    src_ext = write_src("ext.txt", "overwrite-ext")
    refused_ext = capture_file_credential(
        _valid_params(path="scratch/ext.txt", credential_key="ext_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_ext.status == "error"
    assert refused_ext.content["error_code"] == CAPTURE_EXTERNAL_SOURCE_KEY_REFUSED
    assert src_ext.exists()

    # 4. Team key with same name refused.
    team_cred = CatalogCredential(
        display_name="Team Shared Key",
        purpose="Team",
        fields={
            "secret": CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="user_input",
                resolution_order=[CatalogSourceRef(source="sealbox", item_id="team-key:secret", field_ref="value")],
            )
        },
    )
    credential_catalog._save_catalog_credential("shared_team_key", team_cred, scope=team_scope)
    src_team = write_src("team.txt", "team-collision")
    refused_team = capture_file_credential(
        _valid_params(path="scratch/team.txt", credential_key="shared_team_key", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_team.status == "error"
    assert refused_team.content["error_code"] == CAPTURE_TEAM_KEY_REFUSED
    assert src_team.exists()

    # 5. Changed field_name on re-capture of an agent-captured key refused (C05).
    write_src("fn1.txt", "tok-1")
    ok_fn1 = capture_file_credential(
        _valid_params(path="scratch/fn1.txt", credential_key="field_locked", field_name="token", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert ok_fn1.status == "success", ok_fn1.error
    src_fn2 = write_src("fn2.txt", "tok-2")
    refused_fn2 = capture_file_credential(
        _valid_params(path="scratch/fn2.txt", credential_key="field_locked", field_name="secret", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_fn2.status == "error"
    assert refused_fn2.content["error_code"] == CAPTURE_FIELD_NAME_MISMATCH
    assert src_fn2.exists()

    # 6. Shared Sealbox record collision refused.
    monkeypatch.setattr(credential_capture.secrets, "token_hex", lambda _n=8: "fixed00011112222")
    planned_item = "agent-capture:shared_target:fixed00011112222:secret"
    sealbox.secrets[planned_item] = "preexisting-record-secret"
    _seed_registered_sealbox_row(alice_scope, "other_alias", "secret", planned_item)
    src_shared = write_src("shared.txt", "collide")
    refused_shared = capture_file_credential(
        _valid_params(path="scratch/shared.txt", credential_key="shared_target", now=BASE_NOW),
        context=ctx,
        access_gate=gate,
        now=BASE_NOW,
    )
    assert refused_shared.status == "error"
    assert refused_shared.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert refused_shared.content["source_file_deleted"] is False
    assert src_shared.exists()


def test_storage_failure_leaves_file_and_sends_no_notice(env_setup) -> None:
    layout, sealbox, store, gate = env_setup
    sealbox.fail_next_set = True
    source = layout.person_dir(PERSON_ID) / "scratch" / "fail.txt"
    source.write_text("keep-on-error", encoding="utf-8")

    res = capture_file_credential(
        _valid_params(path="scratch/fail.txt", credential_key="fail_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert res.status == "error"
    assert res.content["source_file_deleted"] is False
    assert "left in place" in (res.error or "").lower()
    assert source.exists()
    assert _person_notices(store) == []


def test_post_commit_delete_refuses_if_file_changed_after_read(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _sealbox, _store, gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "swap.txt"
    source.write_text("original-secret", encoding="utf-8")

    real_collect = credential_capture.collect_credential

    def mutate_during_store(*args: Any, **kwargs: Any):
        result = real_collect(*args, **kwargs)
        # Replace source with a brand-new file (new inode) before post-commit unlink runs.
        replacement = source.with_name("swap.tmp")
        replacement.write_text("newer-unrelated-file", encoding="utf-8")
        os.replace(replacement, source)
        return result

    monkeypatch.setattr(credential_capture, "collect_credential", mutate_during_store)

    res = capture_file_credential(
        _valid_params(path="scratch/swap.txt", credential_key="swap_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert res.status == "success", res.error
    assert res.content["source_file_deleted"] is False
    assert res.content["source_file_changed_after_read"] is True
    assert res.content["source_file_already_gone"] is False
    assert res.content["source_file_status"] == SOURCE_FILE_STATUS_CHANGED_AFTER_READ
    assert res.content["source_file_delete_reason"] == "source_file_changed"
    assert source.read_text(encoding="utf-8") == "newer-unrelated-file"


def test_race_between_person_form_and_capture_never_leaves_agent_value_after_form_completes(
    env_setup,
) -> None:
    """C04: whenever the person's form save completes, Sealbox holds the person's value."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    record_key = "team-nats-agents:default:race_key:secret"

    def slow_set(_cmd: list[str], _key: str, _val: str | None) -> None:
        time.sleep(0.02)

    sealbox.on_set_hook = slow_set

    for iteration in range(4):
        credential_catalog._delete_catalog_credential("race_key", scope=alice_scope)
        sealbox.secrets.pop(record_key, None)
        src = layout.person_dir(PERSON_ID) / "scratch" / f"race_{iteration}.txt"
        src.write_text(f"agent-secret-{iteration}", encoding="utf-8")

        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def run_form() -> None:
            barrier.wait()
            outcomes["form"] = collect_credential(
                {
                    "credential_key": "race_key",
                    "display_name": "Race Key",
                    "purpose": "Form save",
                    "store_target": "sealbox",
                    "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
                },
                {"secret": f"person-secret-{iteration}"},
                scope=alice_scope,
                allow_expiry_change=True,
                allow_drop_stored_records=True,
                now=BASE_NOW,
            )

        def run_capture() -> None:
            barrier.wait()
            outcomes["capture"] = capture_file_credential(
                _valid_params(path=f"scratch/race_{iteration}.txt", credential_key="race_key", now=BASE_NOW),
                context=_person_context(),
                access_gate=gate,
                now=BASE_NOW,
            )

        t_form = threading.Thread(target=run_form)
        t_cap = threading.Thread(target=run_capture)
        t_form.start()
        t_cap.start()
        t_form.join(timeout=10)
        t_cap.join(timeout=10)
        assert not t_form.is_alive()
        assert not t_cap.is_alive()

        assert outcomes["form"].status == "success", outcomes["form"].error
        cap_result = outcomes["capture"]
        if cap_result.status == "success":
            assert cap_result.content["source_file_deleted"] is True
            assert not src.exists()
        else:
            assert cap_result.status == "error"
            assert cap_result.content["error_code"] == CAPTURE_PERSON_CREDENTIAL_REFUSED
            assert cap_result.content["source_file_deleted"] is False
            assert src.exists()
        saved = load_catalog(scope=alice_scope).credentials["race_key"]
        assert not is_agent_captured_for_person(saved, PERSON_ID)
        assert saved.approval.get("approved_by") == "user"
        assert sealbox.secrets[record_key] == f"person-secret-{iteration}"


# --- C02, C07, Lead decision: Folder access with real Cedar & auth off --------


@NEEDS_CEDAR
@pytest.mark.asyncio
async def test_folder_access_enforced_with_real_cedar_and_backslash_handled_cleanly(
    env_setup,
) -> None:
    layout, _sealbox, store, _open_gate = env_setup
    gate = _auth_gate(layout, store)
    assert gate.access is not None
    admin = PersonPrincipal("admin-1", TEST_TEAM, ("admin",))

    # Without a grant on procedure:credentials, broker access gate refuses with 403 (B5).
    denied_src = layout.person_dir(PERSON_ID) / "scratch" / "no_grant.txt"
    denied_src.write_text("no-grant-secret", encoding="utf-8")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    expiry_iso = format_utc_timestamp(datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1))
    async with TestClient(TestServer(service.build_app())) as client:
        no_grant_resp = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path="scratch/no_grant.txt", expires_at=expiry_iso),
                "context": agent_context(PERSON_ID, named_agent_type=TEST_AGENT_TYPE),
            },
        )
    assert no_grant_resp.status == 403
    assert denied_src.exists()

    # Grant write on procedure:credentials to alice (person side) and to the team lead
    # (with automatic hand-on for alice's work), and block team/finance for GeneralAssistant.
    gate.access.grant_by_person(
        admin,
        GrantV1(
            grant_id="grant-alice-cred-write",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
            target=TargetRefV1(kind=TargetKind.TOOL, id="procedure:credentials"),
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=BASE_NOW,
            reason="alice may store credentials",
        ),
    )
    gate.access.grant_by_person(
        admin,
        GrantV1(
            grant_id="grant-cred-write",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEST_TEAM, "TeamArchitect")),
            target=TargetRefV1(kind=TargetKind.TOOL, id="procedure:credentials"),
            level=AccessLevel.WRITE,
            delegation_depth=1,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=BASE_NOW,
            reason="allow credential capture",
        ),
    )
    gate.access.grant_by_person(
        admin,
        GrantV1(
            grant_id="block-finance",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEST_TEAM, TEST_AGENT_TYPE)),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.READ,
            kind=GrantKind.BLOCK,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=BASE_NOW,
            reason="block finance folder",
        ),
    )

    # 1. Allowed person folder succeeds.
    allowed_src = layout.person_dir(PERSON_ID) / "scratch" / "allowed.txt"
    allowed_src.write_text("allowed-secret", encoding="utf-8")

    # 2. Blocked team/finance subfolder is refused and file stays in place.
    blocked_src = layout.team_dir / "finance" / "notes.md"
    blocked_src.write_text("finance-secret", encoding="utf-8")

    # 3. Backslash in filename (C07): FolderIdError caught and refused cleanly, file stays in place.
    backslash_src = layout.person_dir(PERSON_ID) / "scratch" / "a\\b.txt"
    backslash_src.write_text("backslash-secret", encoding="utf-8")

    async with TestClient(TestServer(service.build_app())) as client:
        ok_resp = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path="scratch/allowed.txt", credential_key="allowed_key", expires_at=expiry_iso),
                "context": agent_context(PERSON_ID, named_agent_type=TEST_AGENT_TYPE),
            },
        )
        assert ok_resp.status == 200, await ok_resp.text()
        ok_body = await ok_resp.json()

        blocked_resp = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path=str(blocked_src), credential_key="blocked_key", expires_at=expiry_iso),
                "context": agent_context(PERSON_ID, named_agent_type=TEST_AGENT_TYPE),
            },
        )
        blocked_body = await blocked_resp.json()

        bs_resp = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path="scratch/a\\b.txt", credential_key="bs_key", expires_at=expiry_iso),
                "context": agent_context(PERSON_ID, named_agent_type=TEST_AGENT_TYPE),
            },
        )
        bs_body = await bs_resp.json()

    assert ok_resp.status == 200 and ok_body["status"] == "success", ok_body
    assert not allowed_src.exists()

    assert blocked_resp.status == 200
    assert blocked_body["status"] == "error"
    assert blocked_body["content"]["error_code"] == CAPTURE_FOLDER_ACCESS_DENIED
    assert blocked_body["content"]["source_file_deleted"] is False
    assert blocked_src.exists()
    assert "blocked_key" not in load_catalog(scope=CredentialOwnerScope.user(PERSON_ID)).credentials

    assert bs_resp.status == 200
    assert bs_body["status"] == "error"
    assert bs_body["content"]["error_code"] == CAPTURE_FOLDER_ACCESS_DENIED
    assert bs_body["content"]["source_file_deleted"] is False
    assert backslash_src.exists()
    assert "bs_key" not in load_catalog(scope=CredentialOwnerScope.user(PERSON_ID)).credentials


def test_authorization_off_makes_no_folder_decision(env_setup) -> None:
    layout, _sealbox, _store, gate = env_setup
    assert gate.access is None
    blocked_src = layout.team_dir / "finance" / "open_mode.txt"
    blocked_src.write_text("open-mode-secret", encoding="utf-8")

    res = capture_file_credential(
        _valid_params(path=str(blocked_src), credential_key="open_mode_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert res.status == "success", res.error
    assert not blocked_src.exists()


# --- B3: Captured credentials obey PR #199 expiry, janitor, and transfer ------


def test_captured_credential_obeys_immediate_expiry_janitor_and_team_transfer(
    env_setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, sealbox, store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    source = layout.person_dir(PERSON_ID) / "scratch" / "lifecycle.txt"
    source.write_text("lifecycle-secret-value", encoding="utf-8")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    expiry = now + timedelta(minutes=10)
    res = capture_file_credential(
        _valid_params(
            path="scratch/lifecycle.txt",
            credential_key="lifecycle_key",
            field_name="api_key",
            expires_at=format_utc_timestamp(expiry),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res.status == "success", res.error
    assert not source.exists()

    # Team transfer exports and imports the captured credential with expires_at and provenance intact.
    exported = export_credentials(TEST_TEAM, [PERSON_ID])
    entry_records = [r for r in exported if r.get("kind") == "catalog_entry" and r.get("credential_key") == "lifecycle_key"]
    assert len(entry_records) == 1
    assert entry_records[0]["credential"].get("expires_at") == format_utc_timestamp(expiry)
    assert entry_records[0]["credential"].get("approval", {}).get("source") == CAPTURE_APPROVAL_SOURCE

    # Import into a fresh catalog DB and confirm expires_at and capture provenance survive.
    dest_db = tmp_path / "dest_catalog.db"
    orig_db = os.environ["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"]
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(dest_db))
    outcomes = import_credentials(exported, team_id=TEST_TEAM, now=now)
    assert any(o.key.endswith("/catalog_entry/lifecycle_key") and o.imported for o in outcomes)
    imported_cred = load_catalog(scope=alice_scope).credentials["lifecycle_key"]
    assert imported_cred.expires_at == format_utc_timestamp(expiry)
    assert is_agent_captured_for_person(imported_cred, PERSON_ID)
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", orig_db)

    # Link the captured credential to an account tool, then advance past expiry.
    link = store_account_link(store, "http:tavily", "lifecycle_key", owner_id=PERSON_ID)
    assert linked_credential_expired(link, now=now + timedelta(minutes=5)) is False
    assert linked_credential_expired(link, now=expiry + timedelta(seconds=1)) is True

    # Immediate expiry check refuses metadata and resolution after expires_at.
    after = expiry + timedelta(seconds=1)
    meta = get_credential_metadata("lifecycle_key", scope=alice_scope, now=after)
    assert meta.status == "error"
    assert meta.content["error_code"] == CREDENTIAL_EXPIRED
    expired_read = resolve_secret_field("lifecycle_key", "api_key", scope=alice_scope, now=after)
    assert isinstance(expired_read, credential_catalog.ToolResult)
    assert expired_read.status == "error"
    assert expired_read.content["error_code"] == CREDENTIAL_EXPIRED

    # Janitor deletes the expired captured credential, revokes the link, and notifies the person.
    report = clean_expired_credentials(gate, now=after)
    assert report.deleted == 1
    assert report.links_revoked == 1
    assert "lifecycle_key" not in load_catalog(scope=alice_scope).credentials
    assert store.active_link(TEST_TEAM, "http:tavily", link.owner_kind, PERSON_ID, purpose=LinkPurpose.ACCOUNT) is None
    assert not any(k.endswith("lifecycle_key:api_key") for k in sealbox.secrets)


# --- C10, C11, C12, Lead ruling, and B4 settings plumbing ---------------------


def test_capture_without_access_gate_refuses_and_leaves_file_in_place(
    env_setup,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C10 + Lead ruling: missing access_gate must fail closed, log a warning, and keep the file."""

    from secure_tool_broker.executors import execute_validated_action
    from secure_tools.providers import ValidatedAction

    layout, _sealbox, _store, _gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "no_gate.txt"
    source.write_text("no-gate-secret", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        direct = capture_file_credential(
            _valid_params(path="scratch/no_gate.txt", credential_key="no_gate_key", now=BASE_NOW),
            context=_person_context(),
            access_gate=None,
            now=BASE_NOW,
        )
    assert direct.status == "error"
    assert direct.content["error_code"] == CAPTURE_ACCESS_GATE_REQUIRED
    assert direct.content["source_file_deleted"] is False
    assert source.exists()
    assert "no_gate_key" not in load_catalog(scope=CredentialOwnerScope.user(PERSON_ID)).credentials
    assert "credential_capture_access_gate_missing" in caplog.text

    # Also verify execute_validated_action without access_gate fails closed.
    action = ValidatedAction(
        provider="credential_catalog",
        action="credential.capture_file",
        params=_valid_params(
            path="scratch/no_gate.txt",
            credential_key="no_gate_exec_key",
            expires_at=format_utc_timestamp(datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)),
        ),
    )
    via_exec = execute_validated_action(action, _person_context(), access_gate=None)
    assert via_exec.status == "error"
    assert via_exec.content["error_code"] == CAPTURE_ACCESS_GATE_REQUIRED
    assert via_exec.content["source_file_deleted"] is False
    assert source.exists()


def test_second_overwrite_guard_call_refuses_on_concurrent_person_takeover_and_rolls_back_fresh_record(
    env_setup,
) -> None:
    """F1 + F2 + C11: a concurrent person-form takeover mid-write refuses at the second guard call,
    rolls back the fresh Sealbox record cleanly (no partial_store_write), and keeps the prior row,
    prior Sealbox value/TTL, and source file untouched."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)

    def _person_cred(item_id: str) -> CatalogCredential:
        return CatalogCredential(
            display_name="Person Form Credential Added Mid-Write",
            purpose="Concurrent person save",
            approval={"status": "approved", "approved_by": "user", "approved_at": format_utc_timestamp(BASE_NOW)},
            fields={
                "secret": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    value_source="user_input",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_id=item_id, field_ref="value")
                    ],
                )
            },
        )

    # 1. Brand-new key refused at second guard call: fresh Sealbox record is rolled back (deleted).
    new_src = layout.person_dir(PERSON_ID) / "scratch" / "guard_new.txt"
    new_src.write_text("brand-new-secret", encoding="utf-8")
    injected_new = {"done": False, "written_key": ""}

    def inject_person_key_for_new(_cmd: list[str], key: str, _val: str | None) -> None:
        if injected_new["done"]:
            return
        injected_new["done"] = True
        injected_new["written_key"] = key
        sealbox.secrets["team-nats-agents:default:guard_new_race_key:secret"] = "person-secret"
        credential_catalog._save_catalog_credential(
            "guard_new_race_key",
            _person_cred("team-nats-agents:default:guard_new_race_key:secret"),
            scope=alice_scope,
        )

    sealbox.on_set_hook = inject_person_key_for_new
    new_refused = capture_file_credential(
        _valid_params(
            path="scratch/guard_new.txt",
            credential_key="guard_new_race_key",
            expires_at=format_utc_timestamp(BASE_NOW + timedelta(days=30)),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    sealbox.on_set_hook = None
    assert injected_new["done"] is True
    assert new_refused.status == "error"
    assert new_refused.content["error_code"] == CAPTURE_PERSON_CREDENTIAL_REFUSED
    assert new_refused.content["source_file_deleted"] is False
    assert not new_refused.content.get("partial_store_write")
    assert new_src.exists()
    # The freshly written Sealbox record was rolled back and deleted; person's record untouched.
    assert injected_new["written_key"] not in sealbox.secrets
    assert sealbox.secrets["team-nats-agents:default:guard_new_race_key:secret"] == "person-secret"

    # 2. Existing captured key re-captured when a person-form takeover happens mid-write:
    # because re-capture writes to a fresh record name (F2), rollback deletes the fresh
    # record cleanly (partial_store_write is False) and leaves the prior record untouched.
    first_src = layout.person_dir(PERSON_ID) / "scratch" / "guard_first.txt"
    first_src.write_text("initial-secret", encoding="utf-8")
    first_expiry = format_utc_timestamp(BASE_NOW + timedelta(hours=1))
    first = capture_file_credential(
        _valid_params(
            path="scratch/guard_first.txt",
            credential_key="guard_race_key",
            expires_at=first_expiry,
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert first.status == "success", first.error
    assert not first_src.exists()
    first_record_key = (
        load_catalog(scope=alice_scope).credentials["guard_race_key"].fields["secret"].resolution_order[0].item_id
    )
    assert first_record_key is not None
    first_ttl = sealbox.ttls[first_record_key]

    second_src = layout.person_dir(PERSON_ID) / "scratch" / "guard_second.txt"
    second_src.write_text("updated-secret", encoding="utf-8")
    injected = {"done": False, "fresh_key": ""}

    def inject_person_takeover_during_write(_cmd: list[str], key: str, _val: str | None) -> None:
        if injected["done"]:
            return
        injected["done"] = True
        injected["fresh_key"] = key
        credential_catalog._save_catalog_credential(
            "guard_race_key", _person_cred("team-nats-agents:default:guard_race_key:secret"), scope=alice_scope
        )

    sealbox.on_set_hook = inject_person_takeover_during_write
    second = capture_file_credential(
        _valid_params(
            path="scratch/guard_second.txt",
            credential_key="guard_race_key",
            expires_at=format_utc_timestamp(BASE_NOW + timedelta(days=30)),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    sealbox.on_set_hook = None
    assert injected["done"] is True
    assert injected["fresh_key"] != first_record_key
    assert second.status == "error"
    assert second.content["error_code"] == CAPTURE_PERSON_CREDENTIAL_REFUSED
    assert second.content["source_file_deleted"] is False
    assert not second.content.get("partial_store_write")
    assert second_src.exists()
    assert injected["fresh_key"] not in sealbox.secrets
    assert sealbox.secrets[first_record_key] == "initial-secret"
    assert sealbox.ttls[first_record_key] == first_ttl


def test_capture_recaptures_different_layout_for_own_capture_and_never_passes_allow_drop_stored_records(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 + F7: re-capture writes a fresh Sealbox record, commits, and deletes the old record
    ONLY when the old row is this person's own agent capture; capture_file never passes
    allow_drop_stored_records=True, and collect_credential still refuses dropping non-capture rows."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    legacy_item_key = "legacy-captured-layout:secret"
    sealbox.secrets[legacy_item_key] = "legacy-value"
    existing_captured = CatalogCredential(
        display_name="Legacy Captured Layout",
        purpose="Captured under an earlier Sealbox item key",
        expires_at=format_utc_timestamp(BASE_NOW + timedelta(hours=2)),
        approval={
            "status": "captured",
            "source": CAPTURE_APPROVAL_SOURCE,
            "captured_by_agent_type": TEST_AGENT_TYPE,
            "captured_for_person_id": PERSON_ID,
            "captured_at": format_utc_timestamp(BASE_NOW - timedelta(minutes=10)),
        },
        fields={
            "secret": CatalogCredentialField(
                kind="credential_file",
                visibility="secret",
                value_source=CAPTURE_APPROVAL_SOURCE,
                resolution_order=[
                    CatalogSourceRef(source="sealbox", item_id=legacy_item_key, field_ref="value")
                ],
            )
        },
    )
    credential_catalog._save_catalog_credential("layout_diff_key", existing_captured, scope=alice_scope)

    seen_kwargs: list[dict[str, Any]] = []
    real_collect = credential_capture.collect_credential

    def spy_collect(*args: Any, **kwargs: Any):
        seen_kwargs.append(dict(kwargs))
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(credential_capture, "collect_credential", spy_collect)

    src = layout.person_dir(PERSON_ID) / "scratch" / "layout_diff.txt"
    src.write_text("new-secret-value", encoding="utf-8")

    res = capture_file_credential(
        _valid_params(
            path="scratch/layout_diff.txt",
            credential_key="layout_diff_key",
            field_name="secret",
            expires_at=format_utc_timestamp(BASE_NOW + timedelta(hours=4)),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert res.status == "success", res.error
    assert res.content["source_file_deleted"] is True
    assert not src.exists()
    assert len(seen_kwargs) == 1
    assert not seen_kwargs[0].get("allow_drop_stored_records", False)
    assert seen_kwargs[0].get("allow_drop_captured_for_person") == PERSON_ID

    updated_row = load_catalog(scope=alice_scope).credentials["layout_diff_key"]
    new_record_key = updated_row.fields["secret"].resolution_order[0].item_id
    assert new_record_key is not None and new_record_key != legacy_item_key
    assert sealbox.secrets[new_record_key] == "new-secret-value"
    assert legacy_item_key not in sealbox.secrets

    # Even without overwrite_guard, collect_credential with allow_drop_captured_for_person
    # MUST refuse dropping records of a person-entered, external, or team credential.
    person_row = CatalogCredential(
        display_name="Person Row",
        purpose="Form entered",
        approval={"status": "approved", "approved_by": "user"},
        fields={
            "secret": CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="user_input",
                resolution_order=[CatalogSourceRef(source="sealbox", item_id="person-old:secret", field_ref="value")],
            )
        },
    )
    sealbox.secrets["person-old:secret"] = "keep-person-secret"
    credential_catalog._save_catalog_credential("person_no_drop", person_row, scope=alice_scope)

    commands_before = len(sealbox.commands)
    refused_person_drop = real_collect(
        {
            "credential_key": "person_no_drop",
            "display_name": "Person Row",
            "purpose": "Attempt drop",
            "store_target": "sealbox",
            "item_id": "agent-capture:person_no_drop:deadbeef",
            "fields": [{"name": "secret", "kind": "credential_file", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "hijack"},
        scope=alice_scope,
        expires_at=BASE_NOW + timedelta(hours=1),
        allow_expiry_change=True,
        allow_drop_captured_for_person=PERSON_ID,
        now=BASE_NOW,
    )
    assert refused_person_drop.status == "error"
    assert refused_person_drop.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert len(sealbox.commands) == commands_before
    assert sealbox.secrets["person-old:secret"] == "keep-person-secret"

    # Team-scope credential with spoofed capture approval is also refused.
    team_scope = CredentialOwnerScope.team(TEST_TEAM)
    team_row = existing_captured.model_copy(
        update={
            "fields": {
                "secret": CatalogCredentialField(
                    kind="credential_file",
                    visibility="secret",
                    value_source=CAPTURE_APPROVAL_SOURCE,
                    resolution_order=[CatalogSourceRef(source="sealbox", item_id="team-old:secret", field_ref="value")],
                )
            }
        }
    )
    sealbox.secrets["team-old:secret"] = "keep-team-secret"
    credential_catalog._save_catalog_credential("team_no_drop", team_row, scope=team_scope)
    refused_team_drop = real_collect(
        {
            "credential_key": "team_no_drop",
            "display_name": "Team Row",
            "purpose": "Attempt team drop",
            "store_target": "sealbox",
            "item_id": "agent-capture:team_no_drop:deadbeef",
            "fields": [{"name": "secret", "kind": "credential_file", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "hijack"},
        scope=team_scope,
        expires_at=BASE_NOW + timedelta(hours=1),
        allow_expiry_change=True,
        allow_drop_captured_for_person=PERSON_ID,
        now=BASE_NOW,
    )
    assert refused_team_drop.status == "error"
    assert refused_team_drop.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert sealbox.secrets["team-old:secret"] == "keep-team-secret"


def test_operability_messages_folder_check_runtime_error_and_notice_status(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C12 + Lead ruling: moving-clock expiry wording, register_existing_source wording,
    RuntimeError folder-check code/wording, and distinct notice_status values."""

    layout, _sealbox, _store, gate = env_setup

    # 1. Too-soon refusal explains that the earliest time moves with the clock.
    src_soon = layout.person_dir(PERSON_ID) / "scratch" / "soon.txt"
    src_soon.write_text("soon-secret", encoding="utf-8")
    too_soon = capture_file_credential(
        _valid_params(
            path="scratch/soon.txt",
            credential_key="soon_key",
            expires_at=format_utc_timestamp(BASE_NOW + timedelta(seconds=60)),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert too_soon.status == "error"
    assert too_soon.content["error_code"] == CREDENTIAL_EXPIRY_TOO_SOON
    assert "moves with the clock" in str(too_soon.error)
    assert src_soon.exists()

    # 2. register_existing_source over a temporary credential names credential.capture_file.
    src_temp = layout.person_dir(PERSON_ID) / "scratch" / "temp_for_reg.txt"
    src_temp.write_text("temp-secret", encoding="utf-8")
    stored = capture_file_credential(
        _valid_params(path="scratch/temp_for_reg.txt", credential_key="temp_reg_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert stored.status == "success", stored.error
    assert stored.content["notice_sent"] is True
    assert stored.content["notice_status"] == NOTICE_STATUS_SENT

    reg_refused = register_existing_source(
        {
            "credential_key": "temp_reg_key",
            "field_name": "secret",
            "source": "bitwarden",
            "item_id": "bw-1",
            "field_ref": "password",
        },
        scope=CredentialOwnerScope.user(PERSON_ID),
    )
    assert reg_refused.status == "error"
    assert "credential.capture_file" in str(reg_refused.error)

    # 3. RuntimeError from folder decision returns CAPTURE_FOLDER_CHECK_UNAVAILABLE with "could not run".
    src_err = layout.person_dir(PERSON_ID) / "scratch" / "folder_err.txt"
    src_err.write_text("folder-err-secret", encoding="utf-8")

    class _BrokenAccessGate(BrokerAccessGate):
        def decide_folder_for_context(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            raise RuntimeError("cedar helper exited unexpectedly")

    broken_gate = _BrokenAccessGate(
        catalogs=gate.catalogs,
        verifier=gate.verifier,
        team_id=gate.team_id,
        access=object(),  # type: ignore[arg-type]
        links=gate.links,
    )
    with caplog.at_level(logging.WARNING):
        folder_err = capture_file_credential(
            _valid_params(path="scratch/folder_err.txt", credential_key="folder_err_key", now=BASE_NOW),
            context=_person_context(),
            access_gate=broken_gate,
            now=BASE_NOW,
        )
    assert folder_err.status == "error"
    assert folder_err.content["error_code"] == CAPTURE_FOLDER_CHECK_UNAVAILABLE
    assert "could not run" in str(folder_err.error)
    assert folder_err.content["source_file_deleted"] is False
    assert src_err.exists()
    assert "credential_capture_folder_check_failed" in caplog.text

    # 4. notice_status distinguishes "off" vs "failed" (missing store or write error, both logged).
    caplog.clear()
    src_off = layout.person_dir(PERSON_ID) / "scratch" / "notice_off.txt"
    src_off.write_text("notice-off-secret", encoding="utf-8")
    monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, "false")
    res_off = capture_file_credential(
        _valid_params(path="scratch/notice_off.txt", credential_key="notice_off_key", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert res_off.status == "success", res_off.error
    assert res_off.content["notice_sent"] is False
    assert res_off.content["notice_status"] == NOTICE_STATUS_OFF

    monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, "true")
    no_store_gate = BrokerAccessGate(
        catalogs=gate.catalogs,
        verifier=gate.verifier,
        team_id=gate.team_id,
        access=None,
        links=None,
    )
    src_no_store = layout.person_dir(PERSON_ID) / "scratch" / "notice_nostore.txt"
    src_no_store.write_text("notice-nostore-secret", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        res_no_store = capture_file_credential(
            _valid_params(path="scratch/notice_nostore.txt", credential_key="notice_nostore_key", now=BASE_NOW),
            context=_person_context(),
            access_gate=no_store_gate,
            now=BASE_NOW,
        )
    assert res_no_store.status == "success", res_no_store.error
    assert res_no_store.content["notice_sent"] is False
    assert res_no_store.content["notice_status"] == NOTICE_STATUS_FAILED
    assert "credential_capture_notice_unavailable" in caplog.text

    caplog.clear()
    src_fail = layout.person_dir(PERSON_ID) / "scratch" / "notice_fail.txt"
    src_fail.write_text("notice-fail-secret", encoding="utf-8")

    def boom(_notice: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(gate.links, "create_notice_if_absent", boom)
    with caplog.at_level(logging.WARNING):
        res_fail = capture_file_credential(
            _valid_params(path="scratch/notice_fail.txt", credential_key="notice_fail_key", now=BASE_NOW),
            context=_person_context(),
            access_gate=gate,
            now=BASE_NOW,
        )
    assert res_fail.status == "success", res_fail.error
    assert res_fail.content["notice_sent"] is False
    assert res_fail.content["notice_status"] == NOTICE_STATUS_FAILED
    assert "credential_capture_notice_failed" in caplog.text


@pytest.mark.asyncio
async def test_notify_on_agent_capture_setting_schemas_config_loader_and_launch_plumbing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: security_layer.credential_catalog.notify_on_agent_capture across schemas,
    config.example.yaml, config_loader, GatewayInstanceConfig, docker launch, and assistant_cli."""

    import yaml

    from assistant_cli import _run_secure_tool_broker_service
    from assistant_gateway.config import _INSTANCE_CONFIG_COMMENTS, GatewayInstanceConfig
    from config_loader import CONFIG_SCHEMA_VERSION, load_config, resolve_agent_type_settings
    from default_values import SECURITY_LAYER_DEFAULTS
    from docker.launch_headless import build_instance_env_defaults
    from runtime_settings.security import CredentialCatalogSettings

    # 1. Defaults, validation, and instance config comments.
    assert SECURITY_LAYER_DEFAULTS.credential_catalog_notify_on_agent_capture.value is True
    assert CredentialCatalogSettings().notify_on_agent_capture is True
    assert CredentialCatalogSettings(notify_on_agent_capture=False).notify_on_agent_capture is False
    assert CredentialCatalogSettings(notify_on_agent_capture="false").notify_on_agent_capture is False  # type: ignore[arg-type]
    assert CredentialCatalogSettings(notify_on_agent_capture="off").notify_on_agent_capture is False  # type: ignore[arg-type]
    assert CredentialCatalogSettings(notify_on_agent_capture="yes").notify_on_agent_capture is True  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="notify_on_agent_capture"):
        CredentialCatalogSettings(notify_on_agent_capture="maybe")  # type: ignore[arg-type]
    assert "security_layer.credential_catalog.notify_on_agent_capture" in _INSTANCE_CONFIG_COMMENTS

    # 2. config.example.yaml documents it with an inline comment and default true.
    example_text = Path("config.example.yaml").read_text(encoding="utf-8")
    example_data = yaml.safe_load(example_text)
    assert example_data["security_layer"]["credential_catalog"]["notify_on_agent_capture"] is True
    matching_lines = [line for line in example_text.splitlines() if "notify_on_agent_capture:" in line]
    assert len(matching_lines) == 1 and "#" in matching_lines[0]

    # 3. config_loader (config.yaml schema) + env override.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "security_layer": {
                    "mode": "broker",
                    "credential_catalog": {"notify_on_agent_capture": False},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(ENV_NOTIFY_ON_AGENT_CAPTURE, raising=False)
    loaded_cfg = load_config(str(cfg_path))
    assert (
        resolve_agent_type_settings(loaded_cfg, "TeamArchitect")["secure_tool_broker_credential_catalog"][
            "notify_on_agent_capture"
        ]
        is False
    )

    monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, "true")
    loaded_override = load_config(str(cfg_path))
    assert (
        resolve_agent_type_settings(loaded_override, "TeamArchitect")["secure_tool_broker_credential_catalog"][
            "notify_on_agent_capture"
        ]
        is True
    )
    monkeypatch.delenv(ENV_NOTIFY_ON_AGENT_CAPTURE, raising=False)

    # 4. GatewayInstanceConfig save/load + docker/launch_headless env plumbing.
    inst = GatewayInstanceConfig.create(
        instance_name="capture_notice_test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_capture_test",
        timezone="UTC",
    )
    assert inst.security_layer.credential_catalog.notify_on_agent_capture is True
    assert build_instance_env_defaults(inst)[ENV_NOTIFY_ON_AGENT_CAPTURE] == "true"

    inst.security_layer.credential_catalog.notify_on_agent_capture = False
    saved_path = inst.save()
    reloaded = GatewayInstanceConfig.load(saved_path)
    assert reloaded.security_layer.credential_catalog.notify_on_agent_capture is False
    assert build_instance_env_defaults(reloaded)[ENV_NOTIFY_ON_AGENT_CAPTURE] == "false"

    # 5. assistant_cli._run_secure_tool_broker_service exports the setting to the broker env.
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.delenv(ENV_NOTIFY_ON_AGENT_CAPTURE, raising=False)

    async def fake_run_secure_tool_broker(*, host, port, token, **_kwargs):
        observed["notify_env"] = os.environ.get(ENV_NOTIFY_ON_AGENT_CAPTURE)

    monkeypatch.setattr("assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker)
    await _run_secure_tool_broker_service(str(saved_path))
    assert observed["notify_env"] == "false"


def test_notify_on_agent_capture_env_warns_on_unrecognized_value(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C13: invalid env spellings log a warning naming the variable instead of silently falling back."""

    from secure_tool_broker.defaults import notify_on_agent_capture_enabled

    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, truthy)
        assert notify_on_agent_capture_enabled() is True
    for falsy in ("0", "false", "NO", "off"):
        monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, falsy)
        assert notify_on_agent_capture_enabled() is False

    for invalid in ("disabled", "none", "n", "f"):
        caplog.clear()
        monkeypatch.setenv(ENV_NOTIFY_ON_AGENT_CAPTURE, invalid)
        with caplog.at_level(logging.WARNING, logger="secure_tool_broker.defaults"):
            assert notify_on_agent_capture_enabled() is True
        assert any(
            ENV_NOTIFY_ON_AGENT_CAPTURE in rec.getMessage() and invalid in rec.getMessage()
            for rec in caplog.records
        )


@pytest.mark.asyncio
async def test_onboarding_import_and_assets_preserve_notify_on_agent_capture(tmp_path: Path) -> None:
    """C13: onboarding migration import and form JS preserve notify_on_agent_capture=false."""

    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings

    instance_root = tmp_path / "migrated_instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=0,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )
    config_yaml = """
schema_version: 1
agent:
  default_team_id: team_capture_mig
  default_request_timezone: UTC
security_layer:
  mode: broker
  credential_catalog:
    notify_on_agent_capture: false
"""
    async with TestClient(TestServer(service.build_app())) as client:
        import_resp = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "config.yaml", "content": config_yaml},
        )
        imported = await import_resp.json()
        assert import_resp.status == 200
        assert imported["skipped"] == []
        assert imported["settings"]["security_layer"]["credential_catalog"]["notify_on_agent_capture"] is False

        complete_resp = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_capture_mig",
                "team_id": "team_capture_mig",
                "assistant_name": "Nova",
                "user_display_name": "First Admin",
                "timezone": "UTC",
                "model_provider": "OpenAI Compatible",
                "model_id": "test-model",
                "web_chat_auth_token": "uaweb_test_token",
                "generate_internal_credentials": False,
                "security_layer": imported["settings"]["security_layer"],
            },
        )
        assert complete_resp.status == 200

    loaded = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert loaded.security_layer.credential_catalog.notify_on_agent_capture is False

    assets_dir = Path(__file__).resolve().parents[1] / "assistant_gateway" / "onboarding_web" / "assets"
    assert "credentialCatalogNotifyOnAgentCapture" in (assets_dir / "app.js").read_text(encoding="utf-8")
    assert "credential_catalog_notify_on_agent_capture" in (assets_dir / "app_configuration.mjs").read_text(
        encoding="utf-8"
    )
    assert "notify_on_agent_capture: Boolean(sharedState.state.credentialCatalogNotifyOnAgentCapture)" in (
        assets_dir / "app_finalization.mjs"
    ).read_text(encoding="utf-8")


# --- Project-review fix round (F1-F8, F10-F13) -------------------------------


def test_f1_post_write_exception_rolls_back_sealbox_and_team_check_runs_pre_write_only(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: team-key check runs only before writing, and any exception after
    write_credential_to_source (including post-write load_catalog) rolls back
    newly written Sealbox records for both capture_file and base collect_credential."""

    import sqlite3

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    team_scope = CredentialOwnerScope.team(TEST_TEAM)

    # 1. Successful capture calls team load_catalog exactly ONCE (pre-write only).
    team_loads = 0
    real_capture_load = credential_capture.load_catalog

    def count_team_load(*, scope: CredentialOwnerScope | None = None, **kwargs: Any):
        nonlocal team_loads
        if scope == team_scope:
            team_loads += 1
        return real_capture_load(scope=scope, **kwargs)

    monkeypatch.setattr(credential_capture, "load_catalog", count_team_load)
    src_ok = layout.person_dir(PERSON_ID) / "scratch" / "team_once.txt"
    src_ok.write_text("secret-v1", encoding="utf-8")
    first_expiry = format_utc_timestamp(BASE_NOW + timedelta(hours=1))
    first = capture_file_credential(
        _valid_params(path="scratch/team_once.txt", credential_key="f1_key", expires_at=first_expiry, now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert first.status == "success", first.error
    assert team_loads == 1
    first_record_key = (
        load_catalog(scope=alice_scope).credentials["f1_key"].fields["secret"].resolution_order[0].item_id
    )
    assert first_record_key is not None
    first_ttl = sealbox.ttls[first_record_key]

    # 2. Post-write load_catalog raising sqlite3.OperationalError('database is locked')
    # during re-capture rolls back the fresh Sealbox record and keeps the old value + TTL.
    real_catalog_load = credential_catalog.load_catalog
    written_keys: list[str] = []
    fail_after_write = {"armed": False, "raised": False}

    def arm_on_set(_cmd: list[str], key: str, _val: str | None) -> None:
        written_keys.append(key)
        fail_after_write["armed"] = True

    def flaky_load(*, scope: CredentialOwnerScope | None = None, **kwargs: Any):
        if fail_after_write["armed"] and scope == alice_scope:
            fail_after_write["armed"] = False
            fail_after_write["raised"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_catalog_load(scope=scope, **kwargs)

    monkeypatch.setattr(credential_catalog, "load_catalog", flaky_load)
    sealbox.on_set_hook = arm_on_set

    src_recap = layout.person_dir(PERSON_ID) / "scratch" / "f1_recap.txt"
    src_recap.write_text("secret-v2", encoding="utf-8")
    recap_failed = capture_file_credential(
        _valid_params(
            path="scratch/f1_recap.txt",
            credential_key="f1_key",
            expires_at=format_utc_timestamp(BASE_NOW + timedelta(hours=6)),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert fail_after_write["raised"] is True
    assert recap_failed.status == "error"
    assert "database is locked" in (recap_failed.error or "")
    assert recap_failed.content["source_file_deleted"] is False
    assert not recap_failed.content.get("partial_store_write")
    assert src_recap.exists()
    fresh_key = written_keys[-1]
    assert fresh_key != first_record_key
    assert fresh_key not in sealbox.secrets
    assert sealbox.secrets[first_record_key] == "secret-v1"
    assert sealbox.ttls[first_record_key] == first_ttl
    assert real_catalog_load(scope=alice_scope).credentials["f1_key"].expires_at == first_expiry

    # 3. Base collect_credential also rolls back on post-write load_catalog OperationalError.
    written_keys.clear()
    fail_after_write["raised"] = False
    base_failed = collect_credential(
        {
            "credential_key": "f1_base_key",
            "display_name": "Base Key",
            "purpose": "Test post-write load_catalog rollback",
            "store_target": "sealbox",
            "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "base-secret"},
        scope=alice_scope,
        expires_at=BASE_NOW + timedelta(hours=1),
        allow_expiry_change=True,
        now=BASE_NOW,
    )
    sealbox.on_set_hook = None
    assert fail_after_write["raised"] is True
    assert base_failed.status == "error"
    assert not base_failed.content.get("partial_store_write")
    assert written_keys and written_keys[-1] not in sealbox.secrets
    assert "f1_base_key" not in real_catalog_load(scope=alice_scope).credentials


def test_f2_f7_recapture_fresh_record_after_transfer_and_instance_rename_and_save_failure(
    env_setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 + F7: failed re-capture rolls back its fresh record without mutating the old
    captured secret/TTL; re-capture succeeds after team transfer and instance rename,
    committing the fresh record and deleting the old one."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    # Initial capture.
    src1 = layout.person_dir(PERSON_ID) / "scratch" / "f2_v1.txt"
    src1.write_text("v1-secret", encoding="utf-8")
    exp1 = format_utc_timestamp(now + timedelta(hours=1))
    res1 = capture_file_credential(
        _valid_params(path="scratch/f2_v1.txt", credential_key="f2_recap_key", expires_at=exp1, now=now),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res1.status == "success", res1.error
    rec1 = load_catalog(scope=alice_scope).credentials["f2_recap_key"].fields["secret"].resolution_order[0].item_id
    assert rec1 is not None
    ttl1 = sealbox.ttls[rec1]

    # Catalog save failure on re-capture: fresh record is deleted, old record + TTL untouched.
    real_save = credential_catalog._save_catalog_credential

    def fail_save(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disk full on catalog commit")

    monkeypatch.setattr(credential_catalog, "_save_catalog_credential", fail_save)
    src2 = layout.person_dir(PERSON_ID) / "scratch" / "f2_v2.txt"
    src2.write_text("v2-secret", encoding="utf-8")
    res2 = capture_file_credential(
        _valid_params(
            path="scratch/f2_v2.txt",
            credential_key="f2_recap_key",
            expires_at=format_utc_timestamp(now + timedelta(hours=5)),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    monkeypatch.setattr(credential_catalog, "_save_catalog_credential", real_save)
    assert res2.status == "error"
    assert not res2.content.get("partial_store_write")
    assert src2.exists()
    assert sealbox.secrets == {rec1: "v1-secret"}
    assert sealbox.ttls[rec1] == ttl1
    assert load_catalog(scope=alice_scope).credentials["f2_recap_key"].expires_at == exp1

    # F7(a): export and import via team transfer (record name becomes team-transfer:...),
    # then re-capture succeeds and deletes the team-transfer record.
    exported = export_credentials(TEST_TEAM, [PERSON_ID])
    dest_db = tmp_path / "f7_transfer_dest.db"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(dest_db))
    outcomes = import_credentials(exported, team_id=TEST_TEAM, now=now)
    assert any(o.key.endswith("/catalog_entry/f2_recap_key") and o.imported for o in outcomes)
    transfer_rec = (
        load_catalog(scope=alice_scope).credentials["f2_recap_key"].fields["secret"].resolution_order[0].item_id
    )
    assert transfer_rec is not None and transfer_rec.startswith("team-transfer:")
    assert transfer_rec in sealbox.secrets

    # F7(b): also rename TEAM_INSTANCE_NAME before re-capturing.
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "renamed-instance")
    exp2 = format_utc_timestamp(now + timedelta(hours=8))
    res3 = capture_file_credential(
        _valid_params(path="scratch/f2_v2.txt", credential_key="f2_recap_key", expires_at=exp2, now=now),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res3.status == "success", res3.error
    assert not src2.exists()
    fresh_rec = (
        load_catalog(scope=alice_scope).credentials["f2_recap_key"].fields["secret"].resolution_order[0].item_id
    )
    assert fresh_rec is not None and fresh_rec.startswith("agent-capture:f2_recap_key:")
    assert sealbox.secrets[fresh_rec] == "v2-secret"
    assert transfer_rec not in sealbox.secrets


def test_f3_normalized_record_locks_serialize_alias_keys_and_capture_refuses_non_normalized(
    env_setup,
) -> None:
    """F3: capture refuses non-normalized credential_key/field_name; alias catalog keys
    normalizing to the same Sealbox record serialize on normalized record locks; janitor
    deletes and pending dropped-record retries use the same record locks."""

    from secure_tool_broker.credential_catalog import (
        delete_credential,
        retry_pending_dropped_sealbox_deletes,
        sealbox_record_locks,
    )
    from secure_tool_broker.credential_catalog_store import (
        DROPPED_DELETE_QUEUED_BY_FORM,
        CredentialCatalogStore,
        resolve_catalog_db_path,
    )

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    src = layout.person_dir(PERSON_ID) / "scratch" / "norm.txt"
    src.write_text("norm-secret", encoding="utf-8")

    # 1. Capture refuses non-normalized credential_key and field_name.
    for bad in (
        {"credential_key": "tok-"},
        {"credential_key": "tok."},
        {"credential_key": "tok_"},
        {"credential_key": " tok "},
        {"field_name": "sec-"},
        {"field_name": "sec."},
        {"field_name": "sec_"},
        {"field_name": " sec "},
    ):
        params = _valid_params(path="scratch/norm.txt", now=BASE_NOW)
        params.update(bad)
        refused = capture_file_credential(params, context=_person_context(), access_gate=gate, now=BASE_NOW)
        assert refused.status == "error", bad
        assert refused.content["error_code"] == CAPTURE_INVALID_PARAMS, bad
        assert src.exists()

    # 2. Form saving 'tok-' and another temporary write saving 'tok' target the EXACT SAME
    # normalized Sealbox record ('team-nats-agents:default:tok:secret'). Normalized record
    # locks must serialize them so only one writes and the loser is refused pre-write.
    shared_record = "team-nats-agents:default:tok:secret"
    active_writers = {"count": 0, "max_concurrent": 0}
    write_guard = threading.Lock()

    def track_overlap(_cmd: list[str], key: str, _val: str | None) -> None:
        if key != shared_record:
            return
        with write_guard:
            active_writers["count"] += 1
            active_writers["max_concurrent"] = max(active_writers["max_concurrent"], active_writers["count"])
        time.sleep(0.03)
        with write_guard:
            active_writers["count"] -= 1

    sealbox.on_set_hook = track_overlap
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def save_form_dash() -> None:
        barrier.wait()
        results["dash"] = collect_credential(
            {
                "credential_key": "tok-",
                "display_name": "Form Tok Dash",
                "purpose": "Form save",
                "store_target": "sealbox",
                "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
            },
            {"secret": "person-dash-value"},
            scope=alice_scope,
            allow_expiry_change=True,
            allow_drop_stored_records=True,
            now=BASE_NOW,
        )

    def save_temp_plain() -> None:
        barrier.wait()
        results["plain"] = collect_credential(
            {
                "credential_key": "tok",
                "display_name": "Temp Tok Plain",
                "purpose": "Temp save",
                "store_target": "sealbox",
                "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
            },
            {"secret": "temp-plain-value"},
            scope=alice_scope,
            expires_at=BASE_NOW + timedelta(hours=1),
            allow_expiry_change=True,
            now=BASE_NOW,
        )

    t1 = threading.Thread(target=save_form_dash)
    t2 = threading.Thread(target=save_temp_plain)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    sealbox.on_set_hook = None
    assert not t1.is_alive() and not t2.is_alive()
    assert active_writers["max_concurrent"] == 1
    statuses = sorted([results["dash"].status, results["plain"].status])
    assert statuses == ["error", "success"], results
    loser = results["plain"] if results["plain"].status == "error" else results["dash"]
    winner_val = "person-dash-value" if results["dash"].status == "success" else "temp-plain-value"
    assert loser.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert sealbox.secrets[shared_record] == winner_val

    # 3. delete_credential (janitor) and retry_pending_dropped_sealbox_deletes respect
    # sealbox_record_locks on the same normalized record name.
    winner_key = "tok-" if results["dash"].status == "success" else "tok"
    delete_done = threading.Event()

    def run_delete() -> None:
        delete_credential({"credential_key": winner_key}, scope=alice_scope)
        delete_done.set()

    with sealbox_record_locks(alice_scope, [shared_record]):
        t_del = threading.Thread(target=run_delete)
        t_del.start()
        assert not delete_done.wait(timeout=0.08), "delete_credential must wait for sealbox_record_locks"
    t_del.join(timeout=5)
    assert delete_done.is_set()

    # Queue a pending dropped delete and verify retry_pending_dropped_sealbox_deletes waits on the record lock.
    queued_record = "team-nats-agents:default:orphan_rec:secret"
    sealbox.secrets[queued_record] = "orphan-secret"
    CredentialCatalogStore(resolve_catalog_db_path()).put_credential(
        alice_scope,
        "dummy_drop_key",
        CatalogCredential(display_name="Dummy", purpose="Dummy").model_dump(mode="json", exclude_none=True),
        dropped_sealbox_targets=[(queued_record, "secret")],
        queued_by=DROPPED_DELETE_QUEUED_BY_FORM,
    )
    retry_done = threading.Event()

    def run_retry() -> None:
        retry_pending_dropped_sealbox_deletes(scope=alice_scope)
        retry_done.set()

    with sealbox_record_locks(alice_scope, [queued_record]):
        t_retry = threading.Thread(target=run_retry)
        t_retry.start()
        assert not retry_done.wait(timeout=0.08), "retry_pending_dropped_sealbox_deletes must wait for record lock"
    t_retry.join(timeout=5)
    assert retry_done.is_set()
    assert queued_record not in sealbox.secrets


def test_f4_agent_collect_refuses_submitted_secrets_stamps_agent_provenance_and_allows_rotation(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4: agent-callable credential.collect refuses secret values in submitted_fields/fields_values,
    stamps agent provenance (never person approval), keeps store-generated rotation working, and
    never rewrites a captured row (C30: collect over a capture is refused and points to capture_file)."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tools.providers import ValidatedAction

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    # Seed person-entered credential.
    person_save = collect_credential(
        {
            "credential_key": "f4_person_key",
            "display_name": "Person Entered",
            "purpose": "Person form",
            "store_target": "sealbox",
            "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
        },
        {"secret": "person-secret-original"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert person_save.status == "success", person_save.error
    person_rec = (
        load_catalog(scope=alice_scope).credentials["f4_person_key"].fields["secret"].resolution_order[0].item_id
    )
    assert person_rec is not None

    # Seed agent-captured credential (future relative to wall-clock because execute_credential_catalog_action uses utcnow).
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    src_cap = layout.person_dir(PERSON_ID) / "scratch" / "f4_cap.txt"
    src_cap.write_text("captured-secret-v1", encoding="utf-8")
    cap_expiry = format_utc_timestamp(now_utc + timedelta(hours=2))
    cap_res = capture_file_credential(
        _valid_params(
            path="scratch/f4_cap.txt",
            credential_key="f4_captured_key",
            expires_at=cap_expiry,
            now=now_utc,
        ),
        context=ctx,
        access_gate=gate,
        now=now_utc,
    )
    assert cap_res.status == "success", cap_res.error
    cap_rec = (
        load_catalog(scope=alice_scope).credentials["f4_captured_key"].fields["secret"].resolution_order[0].item_id
    )
    assert cap_rec is not None

    # 1. Agent credential.collect with submitted_fields over person-entered key is refused without echoing secret.
    hijack_person = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_person_key",
                    "display_name": "Hijacked",
                    "purpose": "Overwrite person key",
                    "store_target": "sealbox",
                    "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
                },
                "submitted_fields": {"secret": "HIJACK-SECRET-999"},
            },
        ),
        ctx,
    )
    assert hijack_person.status == "error"
    assert hijack_person.content["error_code"] == "secret_value_in_request_rejected"
    assert "HIJACK-SECRET-999" not in (hijack_person.error or "") + json.dumps(dict(hijack_person.content))
    assert sealbox.secrets[person_rec] == "person-secret-original"
    assert load_catalog(scope=alice_scope).credentials["f4_person_key"].approval.get("approved_by") == "user"

    # 2. Agent credential.collect with fields_values over captured key is refused; capture provenance untouched.
    hijack_cap = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_captured_key",
                    "display_name": "Hijacked Cap",
                    "purpose": "Rewrite captured key",
                    "store_target": "sealbox",
                    "fields": [{"name": "secret", "kind": "token", "visibility": "secret", "value_source": "user_input"}],
                },
                "fields_values": {"secret": "HIJACK-CAPTURED-888"},
            },
        ),
        ctx,
    )
    assert hijack_cap.status == "error"
    assert hijack_cap.content["error_code"] == "secret_value_in_request_rejected"
    assert "HIJACK-CAPTURED-888" not in (hijack_cap.error or "") + json.dumps(dict(hijack_cap.content))
    assert sealbox.secrets[cap_rec] == "captured-secret-v1"
    assert is_agent_captured_for_person(load_catalog(scope=alice_scope).credentials["f4_captured_key"], PERSON_ID)

    # 3. Undeclared key in submitted_fields is also refused.
    smuggled = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_smuggle",
                    "display_name": "Smuggle",
                    "purpose": "Test",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"}
                    ],
                },
                "submitted_fields": {"extra_secret": "SMUGGLED-777"},
            },
        ),
        ctx,
    )
    assert smuggled.status == "error"
    assert smuggled.content["error_code"] == "secret_value_in_request_rejected"
    assert "SMUGGLED-777" not in (smuggled.error or "") + json.dumps(dict(smuggled.content))

    # 4. Store-generated creation/rotation with metadata still works and stamps agent provenance
    # (never approved_by="user").
    rotated = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_gen_key",
                    "display_name": "Generated Service",
                    "purpose": "Store generated password",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
                "submitted_fields": {"username": "svc-bot"},
            },
        ),
        ctx,
    )
    assert rotated.status == "success", rotated.error
    gen_row = load_catalog(scope=alice_scope).credentials["f4_gen_key"]
    assert gen_row.approval.get("recorded_by") == "agent"
    assert gen_row.approval.get("agent_type") == "ResearchAgent"
    assert gen_row.approval.get("for_person_id") == PERSON_ID
    assert gen_row.approval.get("approved_by") != "user"

    # 5. Passing item_id on agent credential.collect is refused (C27), and store-generated
    # rotation over a captured row WITHOUT item_id is refused too (C30): the row, its
    # capture provenance and its Sealbox value stay as captured, and the refusal points
    # the agent to credential.capture_file.
    from secure_tool_broker.credential_expiry import (
        CREDENTIAL_CAPTURED_KEY_CAPTURE_ONLY,
        CREDENTIAL_RECORD_NAME_NOT_ALLOWED,
    )

    cap_item_id = cap_rec.rsplit(":", 1)[0]
    cap_row_before = load_catalog(scope=alice_scope).credentials["f4_captured_key"]
    refused_with_item_id = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_captured_key",
                    "display_name": "Rotated Captured",
                    "purpose": "Rotate captured secret via store generation",
                    "store_target": "sealbox",
                    "item_id": cap_item_id,
                    "fields": [
                        {"name": "secret", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert refused_with_item_id.status == "error"
    assert refused_with_item_id.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED

    rotated_cap = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "f4_captured_key",
                    "display_name": "Rotated Captured",
                    "purpose": "Rotate captured secret via store generation",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "secret", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert rotated_cap.status == "error"
    assert rotated_cap.content["error_code"] == CREDENTIAL_CAPTURED_KEY_CAPTURE_ONLY
    assert rotated_cap.content["next_step"] == "credential.capture_file"
    assert "credential.capture_file" in (rotated_cap.error or "")
    unchanged_cap_row = load_catalog(scope=alice_scope).credentials["f4_captured_key"]
    assert unchanged_cap_row.model_dump() == cap_row_before.model_dump()
    assert is_agent_captured_for_person(unchanged_cap_row, PERSON_ID)
    assert sealbox.secrets[cap_rec] == "captured-secret-v1"

    # 6. Agent register_existing_source (first registration of an environment variable)
    # also stamps agent provenance, not approved_by="user".
    monkeypatch.setenv("F4_AGENT_REG_TOKEN", "existing-val")
    reg = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "f4_reg_key",
                "field_name": "token",
                "env_var": "F4_AGENT_REG_TOKEN",
            },
        ),
        ctx,
    )
    assert reg.status == "success", reg.error
    reg_row = load_catalog(scope=alice_scope).credentials["f4_reg_key"]
    assert reg_row.approval.get("recorded_by") == "agent"
    assert reg_row.approval.get("agent_type") == "ResearchAgent"
    assert reg_row.approval.get("for_person_id") == PERSON_ID
    assert reg_row.approval.get("approved_by") != "user"


@NEEDS_CEDAR
@pytest.mark.asyncio
async def test_f5_real_cedar_folder_decision_uses_gate_roles_schedule_id_and_work_id(
    env_setup,
) -> None:
    """F5: real Cedar HTTP tests proving decide_folder_for_context consumes the gate's
    person_roles, schedule_id, and work_id (stripping any of the three breaks the test)."""

    from access_control.entities import role_id, schedule_subject_id
    from access_control.identity import (
        ISSUER_SCHEDULER,
        ContextSigner,
        generate_signing_key,
        verify_keys_value,
    )
    from custom_messages.contracts.access_v2 import GrantLifetime, TaskContextV1
    from tests.broker_access_support import context_signer

    layout, _sealbox, store, _open_gate = env_setup
    (layout.team_dir / "reports").mkdir(parents=True, exist_ok=True)
    (layout.team_dir / "handoff").mkdir(parents=True, exist_ok=True)
    (layout.team_dir / "delegated").mkdir(parents=True, exist_ok=True)

    sched_priv, sched_pub = generate_signing_key(ISSUER_SCHEDULER)
    sched_signer = ContextSigner.from_value(ISSUER_SCHEDULER, sched_priv)
    combined_verify_keys = verify_keys_value([*json.loads(context_verify_keys()), sched_pub])

    catalogs = CatalogSource(layout.root)
    access = AccessService(
        store=store,
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id=TEST_TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
        lead_agent_type="TeamArchitect",
    )
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(combined_verify_keys),
        team_id=TEST_TEAM,
        access=access,
        links=store,
    )
    admin = PersonPrincipal("admin-1", TEST_TEAM, ("admin",))
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    expiry_iso = format_utc_timestamp(now_utc + timedelta(hours=1))

    # Base grants so alice + worker can call procedure:credentials write.
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-alice-cred",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
            target=TargetRefV1(kind=TargetKind.TOOL, id="procedure:credentials"),
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="alice may capture credentials",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-lead-cred",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEST_TEAM, "TeamArchitect")),
            target=TargetRefV1(kind=TargetKind.TOOL, id="procedure:credentials"),
            level=AccessLevel.WRITE,
            delegation_depth=1,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="lead hands on credential capture",
        ),
    )

    # Case 1 (person_roles): team/finance granted on person side ONLY to role 'ops'
    # (standard_grants only covers 'member', so 'viewer' has no grant while 'ops' does).
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-role-ops-finance",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(TEST_TEAM, "ops")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="ops role writes team/finance",
        ),
    )

    # Case 2 (schedule_id):
    # - Positive schedule grant on data/reports (no standard grant for workers).
    # - Schedule-scoped block on team/reports to prove HTTP _admitted_context -> capture_file
    #   propagates schedule_id end-to-end into decide_folder_for_context.
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-alice-data-reports",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="alice writes data/reports",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-sched-cred",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=schedule_subject_id(TEST_TEAM, "nightly-sync")),
            target=TargetRefV1(kind=TargetKind.TOOL, id="procedure:credentials"),
            level=AccessLevel.WRITE,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="schedule captures credentials",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-sched-data-reports",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=schedule_subject_id(TEST_TEAM, "nightly-sync")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.WRITE,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="schedule writes data/reports",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-sched-block-team-reports",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=schedule_subject_id(TEST_TEAM, "nightly-sync")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/reports"),
            level=AccessLevel.WRITE,
            kind=GrantKind.BLOCK,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="nightly-sync cannot write team/reports",
        ),
    )

    # Case 3 (work_id):
    # - Positive task hand-on grant on data/delegated via TeamArchitect delegation_depth=1
    #   (hand_on_for_task requires work_id/task_id and fails without it).
    # - Task-scoped block on team/handoff bound to ctx_a's work_id to prove HTTP
    #   _admitted_context -> capture_file propagates work_id end-to-end.
    ctx_a = context_signer().issue(team_id=TEST_TEAM, person_id=PERSON_ID, person_roles=("member",))
    ctx_b = context_signer().issue(team_id=TEST_TEAM, person_id=PERSON_ID, person_roles=("member",))
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-alice-data-delegated",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/delegated"),
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="alice writes data/delegated",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-lead-data-delegated",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEST_TEAM, "TeamArchitect")),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/delegated"),
            level=AccessLevel.WRITE,
            delegation_depth=1,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="lead delegates data/delegated for one task",
        ),
    )
    access.grant_by_person(
        admin,
        GrantV1(
            grant_id="f5-task-block-handoff",
            team_id=TEST_TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(TEST_TEAM, TEST_AGENT_TYPE)),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/handoff"),
            level=AccessLevel.WRITE,
            kind=GrantKind.BLOCK,
            lifetime=GrantLifetime.TASK,
            task_id=ctx_a.context.context_id,
            for_person_id=PERSON_ID,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=admin.person_id),
            created_at_utc=now_utc,
            reason="task-scoped block on team/handoff for ctx_a",
        ),
    )

    fin_file = layout.team_dir / "finance" / "role.txt"
    fin_file.write_text("role-secret", encoding="utf-8")
    rep_file = layout.team_dir / "reports" / "sched.txt"
    rep_file.write_text("sched-secret", encoding="utf-8")
    ho_file = layout.team_dir / "handoff" / "work.txt"
    ho_file.write_text("work-secret", encoding="utf-8")

    sched_signed = sched_signer.sign(
        TaskContextV1(
            context_id="ctx-sched-1",
            issuer=ISSUER_SCHEDULER,
            team_id=TEST_TEAM,
            person_id=PERSON_ID,
            person_roles=("member",),
            schedule_id="nightly-sync",
            issued_at_utc=now_utc,
            expires_at_utc=now_utc + timedelta(hours=1),
        )
    ).model_dump(mode="json")

    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    try:
        # Verify positive schedule grant and positive task hand-on grant through decide_folder_for_context
        # using the exact ToolContext produced by SecureToolBrokerService._admitted_context.
        from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
        from secure_tools.providers import ValidatedAction

        caller = BrokerCaller(label=CALLER_MANAGER)
        action = ValidatedAction(
            provider="credential_catalog",
            action="credential.capture_file",
            params=_valid_params(path="scratch/x.txt", expires_at=expiry_iso),
        )
        # Positive schedule grant on data/reports: denied without schedule_id, allowed with schedule_id.
        plain_trusted = agent_context(PERSON_ID)
        plain_admitted = service._admitted_context(
            caller,
            action,
            service._request_tool_context({"context": plain_trusted}, plain_trusted, provider="credential_catalog"),
            plain_trusted,
        )
        dec_no_sched = gate.decide_folder_for_context(plain_admitted, "data/reports/nightly.txt")
        assert dec_no_sched is not None and not dec_no_sched.allowed

        sched_trusted = {
            "canonical_user_id": PERSON_ID,
            "principal_type": "user",
            "team_id": TEST_TEAM,
            "agent_id": TEST_AGENT_ID,
            "named_agent_type": TEST_AGENT_TYPE,
            "task_context": sched_signed,
        }
        sched_admitted = service._admitted_context(
            caller,
            action,
            service._request_tool_context({"context": sched_trusted}, sched_trusted, provider="credential_catalog"),
            sched_trusted,
        )
        assert sched_admitted.schedule_id == "nightly-sync"
        dec_with_sched = gate.decide_folder_for_context(sched_admitted, "data/reports/nightly.txt")
        assert dec_with_sched is not None and dec_with_sched.allowed, dec_with_sched

        # Positive task hand-on grant on data/delegated: requires work_id (stripping work_id denies it).
        ctx_a_trusted = {
            "canonical_user_id": PERSON_ID,
            "principal_type": "user",
            "team_id": TEST_TEAM,
            "agent_id": TEST_AGENT_ID,
            "named_agent_type": TEST_AGENT_TYPE,
            "task_context": ctx_a.model_dump(mode="json"),
        }
        ctx_a_admitted = service._admitted_context(
            caller,
            action,
            service._request_tool_context({"context": ctx_a_trusted}, ctx_a_trusted, provider="credential_catalog"),
            ctx_a_trusted,
        )
        assert ctx_a_admitted.work_id == ctx_a.context.context_id
        import dataclasses

        no_work_id_ctx = dataclasses.replace(ctx_a_admitted, work_id=None)
        dec_no_work = gate.decide_folder_for_context(no_work_id_ctx, "data/delegated/item.txt")
        assert dec_no_work is not None and not dec_no_work.allowed
        dec_with_work = gate.decide_folder_for_context(ctx_a_admitted, "data/delegated/item.txt")
        assert dec_with_work is not None and dec_with_work.allowed, dec_with_work

        # Now prove all three fields end-to-end over real HTTP POST /v1/tools/credential_catalog.
        async with TestClient(TestServer(service.build_app())) as client:
            # 1a. viewer role (no standard member grant, no ops grant) denied on team/finance.
            r_viewer = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(fin_file), credential_key="f5_fin", expires_at=expiry_iso),
                        "context": agent_context(PERSON_ID, roles=("viewer",)),
                    },
                )
            ).json()
            assert r_viewer["status"] == "error"
            assert r_viewer["content"]["error_code"] == CAPTURE_FOLDER_ACCESS_DENIED
            assert fin_file.exists()

            # 1b. ops role succeeds on team/finance via the role-dependent grant.
            r_ops = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(fin_file), credential_key="f5_fin", expires_at=expiry_iso),
                        "context": agent_context(PERSON_ID, roles=("ops",)),
                    },
                )
            ).json()
            assert r_ops["status"] == "success", r_ops
            assert not fin_file.exists()

            # 2a. scheduler-signed job run is blocked on team/reports because schedule_id reaches Cedar.
            r_sched_blocked = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(rep_file), credential_key="f5_rep", expires_at=expiry_iso),
                        "context": sched_trusted,
                    },
                )
            ).json()
            assert r_sched_blocked["status"] == "error"
            assert r_sched_blocked["content"]["error_code"] == CAPTURE_FOLDER_ACCESS_DENIED
            assert rep_file.exists()

            # 2b. normal non-scheduled request succeeds on team/reports.
            r_no_sched = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(rep_file), credential_key="f5_rep", expires_at=expiry_iso),
                        "context": agent_context(PERSON_ID),
                    },
                )
            ).json()
            assert r_no_sched["status"] == "success", r_no_sched
            assert not rep_file.exists()

            # 3a. ctx_a (matching work_id) is blocked on team/handoff because work_id reaches Cedar.
            r_blocked_work = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(ho_file), credential_key="f5_ho", expires_at=expiry_iso),
                        "context": ctx_a_trusted,
                    },
                )
            ).json()
            assert r_blocked_work["status"] == "error"
            assert r_blocked_work["content"]["error_code"] == CAPTURE_FOLDER_ACCESS_DENIED
            assert ho_file.exists()

            # 3b. ctx_b (different work_id) succeeds on team/handoff.
            r_other_work = await (
                await client.post(
                    "/v1/tools/credential_catalog",
                    headers=caller_headers(),
                    json={
                        "provider": "credential_catalog",
                        "action": "credential.capture_file",
                        "params": _valid_params(path=str(ho_file), credential_key="f5_ho", expires_at=expiry_iso),
                        "context": {
                            "canonical_user_id": PERSON_ID,
                            "principal_type": "user",
                            "team_id": TEST_TEAM,
                            "agent_id": TEST_AGENT_ID,
                            "named_agent_type": TEST_AGENT_TYPE,
                            "task_context": ctx_b.model_dump(mode="json"),
                        },
                    },
                )
            ).json()
            assert r_other_work["status"] == "success", r_other_work
            assert not ho_file.exists()
    finally:
        access.cedar.close()


def test_f6_not_found_omits_left_in_place_and_secure_cli_marks_capture_and_collect_outcome_unknown(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: capture_file_not_found drops 'left in place'; secure_cli marks both
    credential.capture_file and credential.collect as outcome_unknown on transport errors."""

    from urllib.error import URLError

    from custom_tools import secure_cli_tool

    _layout, _sealbox, _store, gate = env_setup
    not_found = capture_file_credential(
        _valid_params(path="scratch/does_not_exist.txt", now=BASE_NOW),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
    )
    assert not_found.status == "error"
    assert not_found.content["error_code"] == CAPTURE_FILE_NOT_FOUND
    assert "left in place" not in (not_found.error or "").lower()

    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        secure_cli_tool,
        "_trusted_runtime_context",
        lambda: {"canonical_user_id": PERSON_ID, "principal_type": "user"},
    )

    def raise_timeout(_req: Any, **_kwargs: Any) -> Any:
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(secure_cli_tool, "urlopen", raise_timeout)
    for action, params in (
        ("credential.capture_file", _valid_params(path="scratch/x.txt", now=BASE_NOW)),
        ("credential.collect", {"credential_key": "k", "fields": []}),
    ):
        res = secure_cli_tool._secure_cli_impl(
            provider="credential_catalog",
            action=action,
            params=params,
            reason="test timeout",
        )
        assert res["status"] == "error"
        payload = json.loads(res["content"][1]["text"])
        assert payload["error_code"] == "outcome_unknown", action
        assert payload["retry_safe"] is False, action

    # Read action does NOT claim outcome_unknown.
    meta_res = secure_cli_tool._secure_cli_impl(
        provider="credential_catalog",
        action="credential.get_metadata",
        params={"credential_key": "k"},
        reason="check metadata",
    )
    assert meta_res["status"] == "error"
    assert len(meta_res["content"]) == 1


def test_f8_concurrent_same_file_reports_already_gone_not_left_in_place(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8: when two concurrent captures read the same file and one unlinks it first,
    the second reports source_file_already_gone=True / source_file_status='already_gone'."""

    layout, _sealbox, _store, gate = env_setup
    source = layout.person_dir(PERSON_ID) / "scratch" / "concurrent.txt"
    source.write_text("concurrent-secret", encoding="utf-8")

    # Two concurrent captures synchronized right after file read, before store/delete.
    barrier = threading.Barrier(2)
    real_read = credential_capture._read_secret_file

    def synced_read(*args: Any, **kwargs: Any):
        result = real_read(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(credential_capture, "_read_secret_file", synced_read)
    outcomes: list[Any] = []

    def run_one() -> None:
        outcomes.append(
            capture_file_credential(
                _valid_params(path="scratch/concurrent.txt", credential_key="f8_same_file", now=BASE_NOW),
                context=_person_context(),
                access_gate=gate,
                now=BASE_NOW,
            )
        )

    t1 = threading.Thread(target=run_one)
    t2 = threading.Thread(target=run_one)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert len(outcomes) == 2
    assert all(res.status == "success" for res in outcomes), outcomes
    assert not source.exists()
    statuses = sorted(res.content["source_file_status"] for res in outcomes)
    assert statuses == [SOURCE_FILE_STATUS_ALREADY_GONE, SOURCE_FILE_STATUS_DELETED]
    gone = next(res for res in outcomes if res.content["source_file_status"] == SOURCE_FILE_STATUS_ALREADY_GONE)
    assert gone.content["source_file_deleted"] is False
    assert gone.content["source_file_already_gone"] is True
    assert gone.content["source_file_changed_after_read"] is False


@pytest.mark.asyncio
async def test_f11_folder_decision_sqlite_error_returns_unavailable_not_http_500(
    env_setup,
) -> None:
    """F11: sqlite3/storage errors from decide_folder_for_context return
    capture_folder_check_unavailable cleanly instead of HTTP 500."""

    import sqlite3

    layout, _sealbox, _store, gate = env_setup
    src = layout.person_dir(PERSON_ID) / "scratch" / "db_locked.txt"
    src.write_text("keep-on-db-error", encoding="utf-8")

    class _StubAccess:
        lead_agent_type = "TeamArchitect"

        def __init__(self, catalog: Any) -> None:
            self.catalog = catalog

        def agent_role_of(self, _agent_type: str) -> str:
            return "worker"

        def decide(self, **_kwargs: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(allowed=True, reason="", next_step="")

        def decide_folder(self, **_kwargs: Any) -> Any:
            raise sqlite3.OperationalError(f"database is locked: {layout.root}/access.db")

    locked_gate = BrokerAccessGate(
        catalogs=gate.catalogs,
        verifier=gate.verifier,
        team_id=gate.team_id,
        access=_StubAccess(gate.catalogs.current()),  # type: ignore[arg-type]
        links=gate.links,
    )
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=locked_gate)
    expiry_iso = format_utc_timestamp(datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1))
    async with TestClient(TestServer(service.build_app())) as client:
        resp = await client.post(
            "/v1/tools/credential_catalog",
            headers=caller_headers(),
            json={
                "provider": "credential_catalog",
                "action": "credential.capture_file",
                "params": _valid_params(path="scratch/db_locked.txt", credential_key="f11_key", expires_at=expiry_iso),
                "context": agent_context(PERSON_ID),
            },
        )
        body = await resp.json()
    assert resp.status == 200
    assert body["status"] == "error"
    assert body["content"]["error_code"] == CAPTURE_FOLDER_CHECK_UNAVAILABLE
    assert body["content"]["source_file_deleted"] is False
    assert str(layout.root) not in (body.get("error") or "") + json.dumps(body["content"])
    assert src.exists()


def test_f13_capture_refusals_never_return_host_paths(env_setup) -> None:
    """F13: capture refusals name files by folder id and relative path, never host paths."""

    layout, _sealbox, _store, gate = env_setup
    alice_scratch = layout.person_dir(PERSON_ID) / "scratch"
    real = alice_scratch / "real_f13.txt"
    real.write_text("ok", encoding="utf-8")
    link = alice_scratch / "link_f13.txt"
    os.symlink(real, link)
    fifo = alice_scratch / "fifo_f13"
    os.mkfifo(fifo)
    hard = alice_scratch / "hard_f13.txt"
    os.link(real, hard)

    host_markers = (str(layout.root), str(layout.workspaces_dir), str(layout.person_dir(PERSON_ID)))
    refusal_paths = [
        ("scratch/missing_f13.txt", CAPTURE_FILE_NOT_FOUND, True),
        ("scratch/link_f13.txt", CAPTURE_SYMLINK_REFUSED, True),
        ("scratch", CAPTURE_NOT_REGULAR_FILE, True),
        ("scratch/fifo_f13", CAPTURE_NOT_REGULAR_FILE, True),
        ("scratch/hard_f13.txt", CAPTURE_NOT_REGULAR_FILE, True),
        ("../../../team.env", CAPTURE_PATH_OUTSIDE_SCOPE, False),
        (str(layout.root / "team.env"), CAPTURE_PATH_OUTSIDE_SCOPE, False),
        (str(layout.person_dir(PERSON_ID)), CAPTURE_PATH_OUTSIDE_SCOPE, False),
    ]
    for path_arg, expected_code, expect_folder_id in refusal_paths:
        res = capture_file_credential(
            _valid_params(path=path_arg, now=BASE_NOW),
            context=_person_context(),
            access_gate=gate,
            now=BASE_NOW,
        )
        assert res.status == "error", path_arg
        assert res.content["error_code"] == expected_code, (path_arg, res)
        rendered = (res.error or "") + json.dumps(dict(res.content))
        for marker in host_markers:
            assert marker not in rendered, f"host path {marker!r} leaked for {path_arg!r}: {rendered}"
        if expect_folder_id:
            assert f"users/{PERSON_ID}/" in rendered, (path_arg, rendered)


def test_c19_agent_collect_requires_exact_layout_and_refuses_secret_as_metadata_added_removed_and_moved_fields(
    env_setup,
) -> None:
    """C19 (lead ruling on F4): agent-callable credential.collect over an existing key must keep
    the exact field names, secret/non-secret status per field, and stored record references.
    Secret-as-metadata probes (with or without a store-generated field on the same record, and
    against external-source credentials), added fields, removed fields, metadata-as-secret changes,
    and record swaps are all refused pre-write with credential_replacement_form_only; same-layout
    store-generated rotation and existing metadata updates continue to work."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tools.providers import ValidatedAction

    _layout, sealbox, _store, _gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    # Seed person-entered credential with secret field `token`.
    person_save = collect_credential(
        {
            "credential_key": "c19_person_key",
            "display_name": "Person API Token",
            "purpose": "Person form credential",
            "store_target": "sealbox",
            "fields": [
                {"name": "token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"token": "PERSON-REAL-TOKEN-001"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert person_save.status == "success", person_save.error
    token_rec = (
        load_catalog(scope=alice_scope).credentials["c19_person_key"].fields["token"].resolution_order[0].item_id
    )
    assert token_rec is not None

    writes_seen: list[str] = []
    sealbox.on_set_hook = lambda _cmd, key, _val: writes_seen.append(key)

    # 1a. Critic's exact probe (inline value): declare person's secret `token` as metadata + add
    # `keep` as store_generated_secret pointing at `field_ref="token"` so no Sealbox record is dropped.
    probe_inline = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_person_key",
                    "display_name": "Swapped",
                    "purpose": "Attack",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "token",
                            "kind": "custom",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "ATTACKER-TOKEN",
                        },
                        {
                            "name": "keep",
                            "kind": "token",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                            "field_ref": "token",
                        },
                    ],
                },
            },
        ),
        ctx,
    )
    assert probe_inline.status == "error"
    assert probe_inline.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert probe_inline.content["form_creation_action"] == "secure_credential_collection_request"
    assert "secure_credential_collection_request" in (probe_inline.content.get("next_step") or "")
    assert "ATTACKER-TOKEN" not in (probe_inline.error or "") + json.dumps(dict(probe_inline.content))
    assert writes_seen == []
    assert sealbox.secrets[token_rec] == "PERSON-REAL-TOKEN-001"
    resolved_after_inline = resolve_secret_field("c19_person_key", "token", scope=alice_scope)
    assert not isinstance(resolved_after_inline, ToolResult)
    assert resolved_after_inline.value == "PERSON-REAL-TOKEN-001"
    assert resolved_after_inline.secret is True
    assert load_catalog(scope=alice_scope).credentials["c19_person_key"].approval.get("approved_by") == "user"

    # 1b. Critic's exact probe with `ATTACKER-TOKEN` passed in submitted_fields.
    probe_submitted = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_person_key",
                    "display_name": "Swapped Submitted",
                    "purpose": "Attack",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "token",
                            "kind": "custom",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                        },
                        {
                            "name": "keep",
                            "kind": "token",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                            "field_ref": "token",
                        },
                    ],
                },
                "submitted_fields": {"token": "ATTACKER-TOKEN-SUBMITTED"},
            },
        ),
        ctx,
    )
    assert probe_submitted.status == "error"
    assert probe_submitted.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert "ATTACKER-TOKEN-SUBMITTED" not in (probe_submitted.error or "") + json.dumps(dict(probe_submitted.content))
    assert writes_seen == []
    assert sealbox.secrets[token_rec] == "PERSON-REAL-TOKEN-001"

    # 1c. Same-name secret field declared as metadata (without adding a second field),
    # including over a person's external-source (env) credential where no Sealbox record is dropped.
    credential_catalog._save_catalog_credential(
        "c19_ext_key",
        CatalogCredential(
            display_name="External Env Key",
            purpose="External source",
            approval={"status": "approved", "approved_by": "user", "approved_at": format_utc_timestamp(BASE_NOW)},
            fields={
                "token": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[CatalogSourceRef(source="env", env_var="EXT_API_TOKEN")],
                )
            },
        ),
        scope=alice_scope,
    )
    probe_ext = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_ext_key",
                    "display_name": "Hijacked External",
                    "purpose": "Replace secret with metadata",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "token",
                            "kind": "custom",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "ATTACKER-EXT-TOKEN",
                        },
                    ],
                },
            },
        ),
        ctx,
    )
    assert probe_ext.status == "error"
    assert probe_ext.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []
    ext_row = load_catalog(scope=alice_scope).credentials["c19_ext_key"]
    assert ext_row.fields["token"].is_secret() is True
    assert ext_row.fields["token"].resolution_order[0].source == "env"

    # 2. Seed a multi-field person credential (username metadata + password secret + api_token secret).
    multi_save = collect_credential(
        {
            "credential_key": "c19_multi_key",
            "display_name": "Multi Field Service",
            "purpose": "Person multi-field",
            "store_target": "sealbox",
            "fields": [
                {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
                {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
                {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"username": "alice-old", "password": "old-password", "api_token": "old-api-token"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert multi_save.status == "success", multi_save.error
    writes_seen.clear()

    # 2a. Added field (extra metadata field) is refused before writing.
    added_field = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_multi_key",
                    "display_name": "Multi Field Service",
                    "purpose": "Add extra field",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "alice-new"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "extra_note", "kind": "custom", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "extra"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert added_field.status == "error"
    assert added_field.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []

    # 2b. Removed non-secret metadata field (dropping username) is refused before writing.
    removed_field = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_multi_key",
                    "display_name": "Multi Field Service",
                    "purpose": "Remove username",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert removed_field.status == "error"
    assert removed_field.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []

    # 2c. Different Sealbox record (changing item_id or field_ref) is refused before writing.
    moved_record = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_multi_key",
                    "display_name": "Multi Field Service",
                    "purpose": "Change field_ref",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "alice-new"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret", "field_ref": "other_token"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert moved_record.status == "error"
    assert moved_record.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []

    # 2d. Metadata field flipped to secret is refused before writing.
    flipped_metadata = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_multi_key",
                    "display_name": "Multi Field Service",
                    "purpose": "Flip username to secret",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "secret", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert flipped_metadata.status == "error"
    assert flipped_metadata.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []

    # 3. Allowed: exact same layout with store-generated rotation + updated existing metadata succeeds.
    sealbox.on_set_hook = None
    existing_before = load_catalog(scope=alice_scope).credentials["c19_multi_key"]
    expected_records = credential_catalog._credential_sealbox_record_keys(existing_before)
    allowed_rotate = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_multi_key",
                    "display_name": "Multi Field Service",
                    "purpose": "Rotate secrets and update username",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                        {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                },
                "submitted_fields": {"username": "alice-rotated"},
            },
        ),
        ctx,
    )
    assert allowed_rotate.status == "success", allowed_rotate.error
    after_rotate = load_catalog(scope=alice_scope).credentials["c19_multi_key"]
    assert after_rotate.fields["username"].value == "alice-rotated"
    assert credential_catalog._credential_sealbox_record_keys(after_rotate) == expected_records
    assert after_rotate.approval.get("recorded_by") == "agent"
    assert after_rotate.approval.get("approved_by") != "user"

    # 4. Allowed: updating existing non-secret metadata on a metadata-only credential succeeds.
    meta_create = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_meta_only",
                    "display_name": "Metadata Only",
                    "purpose": "Service metadata",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "account_id", "kind": "account_id", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "acct-1"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert meta_create.status == "success", meta_create.error
    meta_update = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c19_meta_only",
                    "display_name": "Metadata Only",
                    "purpose": "Update account_id",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "account_id", "kind": "account_id", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "acct-2"},
                    ],
                },
            },
        ),
        ctx,
    )
    assert meta_update.status == "success", meta_update.error
    assert load_catalog(scope=alice_scope).credentials["c19_meta_only"].fields["account_id"].value == "acct-2"


def test_c20_recapture_old_record_delete_failure_queues_agent_capture_and_retry_cleans_up(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C20: when deleting the old Sealbox record fails during a re-capture, the capture still
    succeeds and deletes its source file, queues the old record with queued_by='agent_capture',
    and retry_pending_dropped_sealbox_deletes removes it once Sealbox answers again."""

    from secure_tool_broker.credential_catalog import retry_pending_dropped_sealbox_deletes
    from secure_tool_broker.credential_catalog_store import (
        DROPPED_DELETE_QUEUED_BY_AGENT_CAPTURE,
        CredentialCatalogStore,
        resolve_catalog_db_path,
    )

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    src1 = layout.person_dir(PERSON_ID) / "scratch" / "c20_v1.txt"
    src1.write_text("c20-secret-v1", encoding="utf-8")
    res1 = capture_file_credential(
        _valid_params(
            path="scratch/c20_v1.txt",
            credential_key="c20_recap_key",
            expires_at=format_utc_timestamp(now + timedelta(hours=1)),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res1.status == "success", res1.error
    rec1 = load_catalog(scope=alice_scope).credentials["c20_recap_key"].fields["secret"].resolution_order[0].item_id
    assert rec1 is not None and rec1 in sealbox.secrets

    # Fail deletion of rec1 during re-capture.
    real_delete = credential_catalog.delete_sealbox_item

    def fail_old_delete(*, item_key: str, item_kind: str, env: Any = None) -> Any:
        if item_key == rec1:
            return ToolResult(status="error", error="simulated sealbox timeout on old record delete")
        return real_delete(item_key=item_key, item_kind=item_kind, env=env)

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fail_old_delete)
    src2 = layout.person_dir(PERSON_ID) / "scratch" / "c20_v2.txt"
    src2.write_text("c20-secret-v2", encoding="utf-8")
    res2 = capture_file_credential(
        _valid_params(
            path="scratch/c20_v2.txt",
            credential_key="c20_recap_key",
            expires_at=format_utc_timestamp(now + timedelta(hours=4)),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", real_delete)
    assert res2.status == "success", res2.error
    assert res2.content["source_file_deleted"] is True
    assert res2.content["source_file_status"] == SOURCE_FILE_STATUS_DELETED
    assert not src2.exists()

    rec2 = load_catalog(scope=alice_scope).credentials["c20_recap_key"].fields["secret"].resolution_order[0].item_id
    assert rec2 is not None and rec2 != rec1
    assert sealbox.secrets[rec2] == "c20-secret-v2"
    assert rec1 in sealbox.secrets

    catalog_store = CredentialCatalogStore(resolve_catalog_db_path())
    pending = catalog_store.list_pending_dropped_sealbox_deletes(
        scope=alice_scope,
        credential_key="c20_recap_key",
    )
    assert len(pending) == 1
    assert pending[0].item_key == rec1
    assert pending[0].queued_by == DROPPED_DELETE_QUEUED_BY_AGENT_CAPTURE
    assert pending[0].queued_by == "agent_capture"

    # Once Sealbox answers again, retry removes rec1 and leaves rec2 intact.
    report = retry_pending_dropped_sealbox_deletes(scope=alice_scope)
    assert report.deleted == 1
    assert report.failed == 0
    assert rec1 not in sealbox.secrets
    assert sealbox.secrets[rec2] == "c20-secret-v2"
    assert catalog_store.list_pending_dropped_sealbox_deletes(scope=alice_scope) == []


def test_c21_partial_write_note_reports_unchanged_earlier_value_and_ttl_expiry(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C21: when rollback deletion of a fresh capture record fails after a post-write failure,
    the error note tells the agent the earlier stored value is unchanged and the unremoved copy
    of the new value will expire with its TTL."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    src1 = layout.person_dir(PERSON_ID) / "scratch" / "c21_v1.txt"
    src1.write_text("c21-secret-v1", encoding="utf-8")
    res1 = capture_file_credential(
        _valid_params(
            path="scratch/c21_v1.txt",
            credential_key="c21_key",
            expires_at=format_utc_timestamp(now + timedelta(hours=1)),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res1.status == "success", res1.error
    rec1 = load_catalog(scope=alice_scope).credentials["c21_key"].fields["secret"].resolution_order[0].item_id
    assert rec1 is not None

    # Fail both catalog commit and Sealbox rollback delete on re-capture.
    monkeypatch.setattr(
        credential_catalog,
        "_save_catalog_credential",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("simulated commit failure")),
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda **_k: ToolResult(status="error", error="simulated rollback delete failure"),
    )
    src2 = layout.person_dir(PERSON_ID) / "scratch" / "c21_v2.txt"
    src2.write_text("c21-secret-v2", encoding="utf-8")
    res2 = capture_file_credential(
        _valid_params(
            path="scratch/c21_v2.txt",
            credential_key="c21_key",
            expires_at=format_utc_timestamp(now + timedelta(hours=2)),
            now=now,
        ),
        context=_person_context(),
        access_gate=gate,
        now=now,
    )
    assert res2.status == "error"
    assert res2.content.get("partial_store_write") is True
    assert "earlier stored value is unchanged" in (res2.error or "")
    assert "expire with its TTL" in (res2.error or "")
    assert "stored value may have changed" not in (res2.error or "").lower()
    assert sealbox.secrets[rec1] == "c21-secret-v1"
    assert src2.exists()


def test_c23_sealbox_record_locks_evict_idle_entries_and_preserve_concurrent_waiters(
    env_setup,
) -> None:
    """C23: _SEALBOX_RECORD_LOCKS removes entries once no thread holds or waits on them so
    repeated captures with fresh record names do not leak memory, while concurrent waiters
    still serialize on the shared lock."""

    from secure_tool_broker.credential_catalog import _SEALBOX_RECORD_LOCKS, sealbox_record_locks

    layout, _sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)

    # Captures and re-captures leave no lingering entries in _SEALBOX_RECORD_LOCKS.
    for idx in range(3):
        src = layout.person_dir(PERSON_ID) / "scratch" / f"c23_{idx}.txt"
        src.write_text(f"secret-{idx}", encoding="utf-8")
        res = capture_file_credential(
            _valid_params(path=f"scratch/c23_{idx}.txt", credential_key="c23_key", now=BASE_NOW),
            context=_person_context(),
            access_gate=gate,
            now=BASE_NOW,
        )
        assert res.status == "success", res.error
    assert _SEALBOX_RECORD_LOCKS == {}

    # Concurrent waiter stays serialized on the same entry and evicts cleanly after both exit.
    active = {"count": 0, "max_active": 0}
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    def holder() -> None:
        with sealbox_record_locks(alice_scope, ["shared:rec"]):
            with guard:
                active["count"] += 1
                active["max_active"] = max(active["max_active"], active["count"])
            entered.set()
            release.wait(timeout=5)
            with guard:
                active["count"] -= 1

    def waiter() -> None:
        assert entered.wait(timeout=5)
        with sealbox_record_locks(alice_scope, ["shared:rec"]):
            with guard:
                active["count"] += 1
                active["max_active"] = max(active["max_active"], active["count"])
                active["count"] -= 1
        second_done.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    assert entered.wait(timeout=5)
    assert not second_done.wait(timeout=0.05)
    assert len(_SEALBOX_RECORD_LOCKS) == 1
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert second_done.is_set()
    assert active["max_active"] == 1
    assert _SEALBOX_RECORD_LOCKS == {}


def test_c25_register_existing_source_enforces_exact_layout_and_allows_new_key_and_metadata_only(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C25 (lead ruling): credential.register_existing_source enforces the same existing-key
    layout rule as credential.collect: critic's Runs 1, 2, and 3 (reclassifying secret as
    metadata with a keep field on Sealbox, over an env-backed key, and re-pointing a field at
    another credential's Sealbox record) are all refused pre-save with rows, approvals, and
    Sealbox values unchanged; first registration of a new key and omitting existing secret
    fields during a metadata update keep working."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tool_broker.credential_expiry import CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    from secure_tools.providers import ValidatedAction

    _layout, sealbox, _store, _gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    # Seed person-entered Sealbox credentials: person_key, service_a, and bank.
    for key_name, secret_val in (
        ("c25_person_key", "PERSON-REAL-SECRET-1"),
        ("c25_service_a", "SERVICE-A-SECRET"),
        ("c25_bank", "BANK-VAULT-SECRET"),
    ):
        saved = collect_credential(
            {
                "credential_key": key_name,
                "display_name": key_name,
                "purpose": "Person form credential",
                "store_target": "sealbox",
                "fields": [
                    {"name": "token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
                ],
            },
            {"token": secret_val},
            scope=alice_scope,
            allow_expiry_change=True,
            allow_drop_stored_records=True,
            now=BASE_NOW,
        )
        assert saved.status == "success", saved.error

    person_rec = (
        load_catalog(scope=alice_scope).credentials["c25_person_key"].fields["token"].resolution_order[0].item_id
    )
    service_a_rec = (
        load_catalog(scope=alice_scope).credentials["c25_service_a"].fields["token"].resolution_order[0].item_id
    )
    bank_rec = (
        load_catalog(scope=alice_scope).credentials["c25_bank"].fields["token"].resolution_order[0].item_id
    )
    assert person_rec and service_a_rec and bank_rec

    # Critic Run 1a (wrapped request) & 1b (flat params): reclassify person's secret `token`
    # as metadata_visible with ATTACKER-TOKEN + add `keep` pointing at person_rec. Since C28
    # the Sealbox source in `keep` is refused before the layout check.
    run1_fields = [
        {
            "name": "token",
            "kind": "custom",
            "visibility": "metadata_visible",
            "value": "ATTACKER-TOKEN",
        },
        {
            "name": "keep",
            "kind": "token",
            "visibility": "secret",
            "source_ref": {"source": "sealbox", "item_id": person_rec, "field_ref": "value"},
        },
    ]
    for params in (
        {"request": {"credential_key": "c25_person_key", "fields": run1_fields}},
        {"credential_key": "c25_person_key", "fields": run1_fields},
    ):
        res = execute_credential_catalog_action(
            ValidatedAction(
                provider="credential_catalog",
                action="credential.register_existing_source",
                params=params,
            ),
            ctx,
        )
        assert res.status == "error"
        assert res.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
        assert res.content["form_creation_action"] == "secure_credential_collection_request"
        assert "ATTACKER-TOKEN" not in (res.error or "") + json.dumps(dict(res.content))
    row_person = load_catalog(scope=alice_scope).credentials["c25_person_key"]
    assert row_person.approval.get("approved_by") == "user"
    assert set(row_person.fields) == {"token"}
    assert row_person.fields["token"].is_secret() is True
    assert sealbox.secrets[person_rec] == "PERSON-REAL-SECRET-1"
    resolved_person = resolve_secret_field("c25_person_key", "token", scope=alice_scope)
    assert not isinstance(resolved_person, ToolResult)
    assert resolved_person.value == "PERSON-REAL-SECRET-1"
    assert resolved_person.secret is True

    # Critic Run 2: over a person's env-backed credential, reclassifying `token` as metadata
    # with one field is refused and leaves row and approval unchanged.
    monkeypatch.setenv("C25_PERSON_ENV_TOKEN", "ENV-REAL-SECRET")
    credential_catalog._save_catalog_credential(
        "c25_env_key",
        CatalogCredential(
            display_name="Env Key",
            purpose="Env credential",
            approval={"status": "approved", "approved_by": "user", "approved_at": format_utc_timestamp(BASE_NOW)},
            fields={
                "token": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[CatalogSourceRef(source="env", env_var="C25_PERSON_ENV_TOKEN")],
                ),
            },
        ),
        scope=alice_scope,
    )
    run2 = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_env_key",
                "fields": [
                    {
                        "name": "token",
                        "kind": "custom",
                        "visibility": "metadata_visible",
                        "value": "ATTACKER-ENV-TOKEN",
                    }
                ],
            },
        ),
        ctx,
    )
    assert run2.status == "error"
    assert run2.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    row_env = load_catalog(scope=alice_scope).credentials["c25_env_key"]
    assert row_env.approval.get("approved_by") == "user"
    assert row_env.fields["token"].is_secret() is True
    assert row_env.fields["token"].resolution_order[0].env_var == "C25_PERSON_ENV_TOKEN"

    # Critic Run 3: re-pointing service_a's token at bank's Sealbox record (both with and
    # without a keep field) is refused (since C28 as a Sealbox source); service_a and bank rows,
    # approvals, and values stay intact.
    for fields in (
        [
            {
                "name": "token",
                "kind": "token",
                "visibility": "secret",
                "source_ref": {"source": "sealbox", "item_id": bank_rec, "field_ref": "value"},
            },
            {
                "name": "keep",
                "kind": "token",
                "visibility": "secret",
                "source_ref": {"source": "sealbox", "item_id": service_a_rec, "field_ref": "value"},
            },
        ],
        [
            {
                "name": "token",
                "kind": "token",
                "visibility": "secret",
                "source_ref": {"source": "sealbox", "item_id": bank_rec, "field_ref": "value"},
            },
        ],
    ):
        run3 = execute_credential_catalog_action(
            ValidatedAction(
                provider="credential_catalog",
                action="credential.register_existing_source",
                params={"credential_key": "c25_service_a", "fields": fields},
            ),
            ctx,
        )
        assert run3.status == "error"
        assert run3.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    row_a = load_catalog(scope=alice_scope).credentials["c25_service_a"]
    assert row_a.approval.get("approved_by") == "user"
    assert row_a.fields["token"].resolution_order[0].item_id == service_a_rec
    assert sealbox.secrets[service_a_rec] == "SERVICE-A-SECRET"
    assert sealbox.secrets[bank_rec] == "BANK-VAULT-SECRET"

    # Inline value on a secret field and sealbox_key are also refused.
    secret_val_refused = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_new_bad",
                "fields": [
                    {
                        "name": "token",
                        "kind": "token",
                        "visibility": "secret",
                        "env_var": "C25_PERSON_ENV_TOKEN",
                        "value": "SMUGGLED-SECRET",
                    }
                ],
            },
        ),
        ctx,
    )
    assert secret_val_refused.status == "error"
    assert secret_val_refused.content["error_code"] == "secret_value_in_request_rejected"

    sb_key_refused = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_new_bad2",
                "sealbox_key": "attacker-chosen",
                "field_name": "token",
                "env_var": "C25_PERSON_ENV_TOKEN",
            },
        ),
        ctx,
    )
    assert sb_key_refused.status == "error"
    assert sb_key_refused.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED

    # First registration of a new env key keeps working (a new uncataloged Sealbox record no longer does, C28).
    new_env = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_first_env",
                "field_name": "token",
                "env_var": "C25_PERSON_ENV_TOKEN",
            },
        ),
        ctx,
    )
    assert new_env.status == "success", new_env.error
    assert load_catalog(scope=alice_scope).credentials["c25_first_env"].approval.get("recorded_by") == "agent"

    sealbox.secrets["c25-uncataloged:token"] = "UNCATALOGED-SECRET"
    new_sb = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_first_sealbox",
                "field_name": "token",
                "source": "sealbox",
                "item_id": "c25-uncataloged:token",
                "field_ref": "value",
            },
        ),
        ctx,
    )
    # C28: an uncataloged Sealbox record is no longer registrable by an agent.
    assert new_sb.status == "error"
    assert new_sb.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert new_sb.content["rejected_source"] == "sealbox"
    assert "c25_first_sealbox" not in load_catalog(scope=alice_scope).credentials
    assert sealbox.secrets["c25-uncataloged:token"] == "UNCATALOGED-SECRET"

    # Metadata update over an existing key via register_existing_source omitting the secret field
    # updates metadata and keeps the secret field untouched.
    meta_seed = collect_credential(
        {
            "credential_key": "c25_meta_and_secret",
            "display_name": "Service Login",
            "purpose": "Seed login",
            "store_target": "sealbox",
            "fields": [
                {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
                {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"username": "old-user", "password": "KEEP-THIS-PASSWORD"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert meta_seed.status == "success", meta_seed.error
    reg_meta_only = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c25_meta_and_secret",
                "fields": [
                    {"name": "username", "kind": "username", "visibility": "metadata_visible", "value": "new-user"},
                ],
            },
        ),
        ctx,
    )
    assert reg_meta_only.status == "success", reg_meta_only.error
    updated_row = load_catalog(scope=alice_scope).credentials["c25_meta_and_secret"]
    assert updated_row.fields["username"].value == "new-user"
    resolved_pw = resolve_secret_field("c25_meta_and_secret", "password", scope=alice_scope)
    assert not isinstance(resolved_pw, ToolResult)
    assert resolved_pw.value == "KEEP-THIS-PASSWORD"


def test_c26_agent_metadata_update_keeps_omitted_secret_untouched_while_rotation_and_form_rules_hold(
    env_setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C26 (lead ruling): agent-callable credential.collect may omit existing secret fields
    to update existing non-secret metadata without touching Sealbox or replacing the person's
    secrets; optional blank secret fields and adding fields (even on an agent-created key) are
    refused with clear guidance; partial and full store-generated rotation still work."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tools.providers import ValidatedAction

    _layout, sealbox, _store, _gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    # Seed person-entered credential with username + password + api_token.
    saved = collect_credential(
        {
            "credential_key": "c26_login",
            "display_name": "Service Login",
            "purpose": "Person login",
            "store_target": "sealbox",
            "fields": [
                {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
                {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
                {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"username": "alice-old", "password": "PERSON-PW-ORIGINAL", "api_token": "PERSON-TOKEN-ORIGINAL"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert saved.status == "success", saved.error
    before_row = load_catalog(scope=alice_scope).credentials["c26_login"]
    pw_rec = before_row.fields["password"].resolution_order[0].item_id
    tok_rec = before_row.fields["api_token"].resolution_order[0].item_id
    assert pw_rec and tok_rec

    writes_seen: list[str] = []
    sealbox.on_set_hook = lambda _cmd, key, _val: writes_seen.append(key)

    # 1. Updating username ONLY (omitting both password and api_token) succeeds with ZERO
    # Sealbox writes and leaves both secrets completely untouched.
    meta_only = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_login",
                    "display_name": "Service Login",
                    "purpose": "Correct username only",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "alice-corrected",
                        }
                    ],
                }
            },
        ),
        ctx,
    )
    assert meta_only.status == "success", meta_only.error
    assert writes_seen == []
    assert meta_only.content["stored_secret_fields"] == []
    after_meta = load_catalog(scope=alice_scope).credentials["c26_login"]
    assert after_meta.fields["username"].value == "alice-corrected"
    assert after_meta.fields["password"].resolution_order == before_row.fields["password"].resolution_order
    assert after_meta.fields["api_token"].resolution_order == before_row.fields["api_token"].resolution_order
    resolved_pw = resolve_secret_field("c26_login", "password", scope=alice_scope)
    resolved_tok = resolve_secret_field("c26_login", "api_token", scope=alice_scope)
    assert not isinstance(resolved_pw, ToolResult) and resolved_pw.value == "PERSON-PW-ORIGINAL"
    assert not isinstance(resolved_tok, ToolResult) and resolved_tok.value == "PERSON-TOKEN-ORIGINAL"

    # 2. Sending an existing secret field as an optional blank field is still refused (must omit
    # to leave untouched, or use store_generated_secret to rotate).
    blank_opt = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_login",
                    "display_name": "Service Login",
                    "purpose": "Blank optional password",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "alice-blank",
                        },
                        {
                            "name": "password",
                            "kind": "password",
                            "visibility": "secret",
                            "required": False,
                            "value_source": "user_input",
                        },
                    ],
                }
            },
        ),
        ctx,
    )
    assert blank_opt.status == "error"
    assert blank_opt.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert "omitting existing secret fields to leave them untouched" in (blank_opt.error or "")
    assert writes_seen == []
    assert load_catalog(scope=alice_scope).credentials["c26_login"].fields["username"].value == "alice-corrected"

    # 3. Rotating ONLY api_token while omitting password writes ONLY api_token to Sealbox and
    # keeps password's original person-entered value untouched.
    partial_rotate = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_login",
                    "display_name": "Service Login",
                    "purpose": "Rotate api_token only",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "alice-corrected",
                        },
                        {
                            "name": "api_token",
                            "kind": "token",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                        },
                    ],
                }
            },
        ),
        ctx,
    )
    assert partial_rotate.status == "success", partial_rotate.error
    assert writes_seen == [tok_rec]
    assert partial_rotate.content["stored_secret_fields"] == ["api_token"]
    resolved_pw_after = resolve_secret_field("c26_login", "password", scope=alice_scope)
    resolved_tok_after = resolve_secret_field("c26_login", "api_token", scope=alice_scope)
    assert not isinstance(resolved_pw_after, ToolResult) and resolved_pw_after.value == "PERSON-PW-ORIGINAL"
    assert not isinstance(resolved_tok_after, ToolResult) and resolved_tok_after.value != "PERSON-TOKEN-ORIGINAL"

    # 4. Metadata update on an env-backed credential while omitting its secret field succeeds
    # without needing Sealbox writes.
    writes_seen.clear()
    monkeypatch.setenv("C26_ENV_SECRET", "ENV-SECRET-VALUE")
    credential_catalog._save_catalog_credential(
        "c26_env_cred",
        CatalogCredential(
            display_name="Env Login",
            purpose="Env credential",
            approval={"status": "approved", "approved_by": "user", "approved_at": format_utc_timestamp(BASE_NOW)},
            fields={
                "username": CatalogCredentialField(
                    kind="username",
                    visibility="metadata_visible",
                    value_source="user_input",
                    value="env-old",
                ),
                "token": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[CatalogSourceRef(source="env", env_var="C26_ENV_SECRET")],
                ),
            },
        ),
        scope=alice_scope,
    )
    env_meta = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_env_cred",
                    "display_name": "Env Login",
                    "purpose": "Update env username",
                    "fields": [
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "env-new",
                        }
                    ],
                }
            },
        ),
        ctx,
    )
    assert env_meta.status == "success", env_meta.error
    assert writes_seen == []
    assert load_catalog(scope=alice_scope).credentials["c26_env_cred"].fields["username"].value == "env-new"
    resolved_env = resolve_secret_field("c26_env_cred", "token", scope=alice_scope)
    assert not isinstance(resolved_env, ToolResult) and resolved_env.value == "ENV-SECRET-VALUE"

    # 5. Critic's test_e: adding a new field to an agent-created credential is still refused
    # (adding a field always needs the secure form).
    sealbox.on_set_hook = None
    created = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_agent_made",
                    "display_name": "Agent Service",
                    "purpose": "Agent generated login",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "bot-1"},
                        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "store_generated_secret"},
                    ],
                }
            },
        ),
        ctx,
    )
    assert created.status == "success", created.error
    writes_seen.clear()
    sealbox.on_set_hook = lambda _cmd, key, _val: writes_seen.append(key)
    add_field = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c26_agent_made",
                    "display_name": "Agent Service",
                    "purpose": "Try to add account_id",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "bot-1"},
                        {"name": "account_id", "kind": "account_id", "visibility": "metadata_visible", "value_source": "agent_supplied_metadata", "value": "acct-9"},
                    ],
                }
            },
        ),
        ctx,
    )
    assert add_field.status == "error"
    assert add_field.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert writes_seen == []


def test_c27_agent_saves_refuse_record_name_choice_and_shared_sealbox_records(
    env_setup,
) -> None:
    """C27 (lead ruling): agent-callable saves never choose Sealbox record names (item_id and
    sealbox_key in agent credential.collect are refused) and refuse any Sealbox record already
    referenced by another catalog key (temporary_only=False) in both credential.collect and
    credential.register_existing_source."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tool_broker.credential_expiry import (
        CREDENTIAL_EXPIRY_SHARED_RECORD,
        CREDENTIAL_RECORD_NAME_NOT_ALLOWED,
    )
    from secure_tools.providers import ValidatedAction

    _layout, sealbox, _store, _gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    # Seed permanent person-entered credential `c27_person_key`.
    saved = collect_credential(
        {
            "credential_key": "c27_person_key",
            "display_name": "Person Key",
            "purpose": "Permanent person credential",
            "store_target": "sealbox",
            "fields": [
                {"name": "token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"token": "PERSON-PERMANENT-SECRET"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert saved.status == "success", saved.error
    person_row_before = load_catalog(scope=alice_scope).credentials["c27_person_key"]
    person_rec = person_row_before.fields["token"].resolution_order[0].item_id
    assert person_rec is not None
    person_prefix = person_rec.rsplit(":", 1)[0]

    writes_seen: list[str] = []
    sealbox.on_set_hook = lambda _cmd, key, _val: writes_seen.append(key)

    # 1. Critic's test_f: new key `other_key` with sealbox_key or item_id targeting person_key's
    # record prefix is refused before any Sealbox write (in request, top-level params, or field spec).
    for bad_params in (
        {
            "request": {
                "credential_key": "c27_other_key",
                "display_name": "Other",
                "purpose": "Hijack via sealbox_key",
                "store_target": "sealbox",
                "sealbox_key": person_prefix,
                "fields": [
                    {"name": "token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"}
                ],
            }
        },
        {
            "request": {
                "credential_key": "c27_other_key",
                "display_name": "Other",
                "purpose": "Hijack via item_id",
                "store_target": "sealbox",
                "item_id": person_prefix,
                "fields": [
                    {"name": "token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"}
                ],
            }
        },
        {
            "item_id": person_prefix,
            "request": {
                "credential_key": "c27_other_key",
                "display_name": "Other",
                "purpose": "Hijack via top-level item_id",
                "store_target": "sealbox",
                "fields": [
                    {"name": "token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"}
                ],
            },
        },
        {
            "request": {
                "credential_key": "c27_other_key",
                "display_name": "Other",
                "purpose": "Hijack via field item_id",
                "store_target": "sealbox",
                "fields": [
                    {
                        "name": "token",
                        "kind": "token",
                        "visibility": "secret",
                        "value_source": "store_generated_secret",
                        "item_id": person_prefix,
                    }
                ],
            }
        },
    ):
        res = execute_credential_catalog_action(
            ValidatedAction(
                provider="credential_catalog",
                action="credential.collect",
                params=bad_params,
            ),
            ctx,
        )
        assert res.status == "error"
        assert res.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert writes_seen == []
    assert "c27_other_key" not in load_catalog(scope=alice_scope).credentials
    person_row_after = load_catalog(scope=alice_scope).credentials["c27_person_key"]
    assert person_row_after.updated_at == person_row_before.updated_at
    assert person_row_after.approval == person_row_before.approval
    assert sealbox.secrets[person_rec] == "PERSON-PERMANENT-SECRET"

    # 2. Shared-record guard (temporary_only=False) even WITHOUT item_id/sealbox_key:
    # person saves `c27_holder` on the default Sealbox record prefix of `c27_collide`, then
    # agent calls credential.collect for new key `c27_collide` with no item_id/sealbox_key.
    sealbox.on_set_hook = None
    collide_prefix = credential_catalog._sealbox_catalog_key("c27_collide", {})
    holder_save = collect_credential(
        {
            "credential_key": "c27_holder",
            "display_name": "Holder",
            "purpose": "Permanent holder",
            "store_target": "sealbox",
            "item_id": collide_prefix,
            "fields": [
                {"name": "token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
            ],
        },
        {"token": "HOLDER-SECRET"},
        scope=alice_scope,
        allow_expiry_change=True,
        allow_drop_stored_records=True,
        now=BASE_NOW,
    )
    assert holder_save.status == "success", holder_save.error
    holder_rec = load_catalog(scope=alice_scope).credentials["c27_holder"].fields["token"].resolution_order[0].item_id
    assert holder_rec is not None

    writes_seen.clear()
    sealbox.on_set_hook = lambda _cmd, key, _val: writes_seen.append(key)
    colliding_collect = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.collect",
            params={
                "request": {
                    "credential_key": "c27_collide",
                    "display_name": "Colliding Key",
                    "purpose": "Should refuse shared Sealbox record",
                    "store_target": "sealbox",
                    "fields": [
                        {"name": "token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"}
                    ],
                }
            },
        ),
        ctx,
    )
    assert colliding_collect.status == "error"
    assert colliding_collect.content["error_code"] == CREDENTIAL_EXPIRY_SHARED_RECORD
    assert colliding_collect.content["conflicting_credential_key"] == "c27_holder"
    assert writes_seen == []
    assert "c27_collide" not in load_catalog(scope=alice_scope).credentials
    assert sealbox.secrets[holder_rec] == "HOLDER-SECRET"

    # 3. register_existing_source for a new key referencing permanent c27_person_key's Sealbox
    # record is also refused (since C28 every Sealbox source is refused for agents).
    aliasing_reg = execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.register_existing_source",
            params={
                "credential_key": "c27_alias_reg",
                "field_name": "token",
                "source": "sealbox",
                "item_id": person_rec,
                "field_ref": "value",
            },
        ),
        ctx,
    )
    assert aliasing_reg.status == "error"
    assert aliasing_reg.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert "c27_alias_reg" not in load_catalog(scope=alice_scope).credentials
    assert sealbox.secrets[person_rec] == "PERSON-PERMANENT-SECRET"





class _FakeBitwarden:
    """In-memory ``bw`` double: items by name, create/edit/get only."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.counter = 0

    def run(self, command: list[str], *, input_text: str | None = None, **_kwargs: Any) -> Any:
        if command[1:3] == ["get", "item"]:
            item = self.items.get(command[3])
            if item is None:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="Not found.")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(item), stderr="")
        if command[1:3] in (["create", "item"], ["edit", "item"]):
            payload = json.loads(input_text or "{}")
            if command[1] == "create":
                self.counter += 1
                payload["id"] = f"bw-{self.counter}"
            else:
                payload["id"] = command[3]
            self.items[payload["name"]] = payload
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


def test_c28_c29_c30_agent_saves_never_replace_records_they_do_not_own(
    env_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C28-C30 (lead rulings). Agent register_existing_source never registers a Sealbox source and
    never names a password-manager item another key uses; agent credential.collect refuses
    item_name, never rotates a registered existing source, never writes into a Bitwarden item that
    another key uses or that already exists for a new key, checks the username of every rotated
    username-and-password record before the first write, and refuses a captured row with a next
    step pointing to credential.capture_file. First registration of an env var still works."""

    from secure_tool_broker.credential_catalog import execute_credential_catalog_action
    from secure_tool_broker.credential_expiry import (
        CREDENTIAL_CAPTURED_KEY_CAPTURE_ONLY,
        CREDENTIAL_RECORD_NAME_NOT_ALLOWED,
        CREDENTIAL_ROTATION_USERNAME_REQUIRED,
    )
    from secure_tools.providers import ValidatedAction

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    ctx = _person_context(agent_type="ResearchAgent")

    def act(action: str, params: dict[str, Any]) -> ToolResult:
        return execute_credential_catalog_action(
            ValidatedAction(provider="credential_catalog", action=action, params=params), ctx
        )

    def sealbox_sets() -> list[list[str]]:
        return [c for c in sealbox.commands if c[:2] in (["secret", "set"], ["credential", "set"])]

    generated_token = {"name": "token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"}

    # --- C28 Sealbox: the critic's sign-in run. The person's GitHub sign-in lives in an
    # uncataloged Sealbox record of their tenant.
    sign_in_record = "team-cred-store:github:token"
    sealbox.secrets[sign_in_record] = "gho_person_sign_in_token"
    sealbox.commands.clear()
    step1 = act(
        "credential.register_existing_source",
        {"credential_key": "gh_alias", "field_name": "token", "source": "sealbox",
         "item_id": sign_in_record, "field_ref": "value"},
    )
    assert step1.status == "error"
    assert step1.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert step1.content["rejected_source"] == "sealbox"
    # The same refusal for the wrapped request, a source_refs mapping and the CLI alias.
    for params in (
        {"request": {"credential_key": "gh_alias", "fields": [
            {"name": "token", "kind": "token", "visibility": "secret",
             "source_ref": {"source": "sealbox-cli", "item_id": sign_in_record, "field_ref": "value"}}]}},
        {"credential_key": "gh_alias",
         "fields": [{"name": "token", "kind": "token", "visibility": "secret"}],
         "source_refs": {"token": {"source": "sealbox", "item_id": sign_in_record, "field_ref": "value"}}},
    ):
        again = act("credential.register_existing_source", params)
        assert again.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED, again.error
    assert "gh_alias" not in load_catalog(scope=alice_scope).credentials
    # Step 2 against a row registered before this rule: rotating a registered source is refused.
    _seed_registered_sealbox_row(alice_scope, "gh_alias", "token", sign_in_record)
    old_row = load_catalog(scope=alice_scope).credentials["gh_alias"]
    step2 = act(
        "credential.collect",
        {"request": {"credential_key": "gh_alias", "display_name": "gh_alias", "purpose": "rotate",
                     "store_target": "sealbox", "fields": [generated_token]}},
    )
    assert step2.status == "error"
    assert step2.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert "existing source" in (step2.error or "")
    assert sealbox.secrets[sign_in_record] == "gho_person_sign_in_token"
    assert sealbox_sets() == []
    assert load_catalog(scope=alice_scope).credentials["gh_alias"].model_dump() == old_row.model_dump()

    # An env-backed key registered by an agent is first-registration only: it can never be rotated.
    monkeypatch.setenv("C28_FIRST_ENV_TOKEN", "ENV-VALUE")
    first_env = act(
        "credential.register_existing_source",
        {"credential_key": "c28_env", "field_name": "token", "env_var": "C28_FIRST_ENV_TOKEN"},
    )
    assert first_env.status == "success", first_env.error
    env_rotate = act(
        "credential.collect",
        {"request": {"credential_key": "c28_env", "display_name": "c28_env", "purpose": "rotate",
                     "store_target": "sealbox", "fields": [generated_token]}},
    )
    assert env_rotate.content["error_code"] == CREDENTIAL_REPLACEMENT_FORM_ONLY
    assert resolve_secret_field("c28_env", "token", scope=alice_scope).value == "ENV-VALUE"
    assert sealbox_sets() == []

    # --- C29: the critic's two-secret rotation with the kinds redeclared. Refused before any write.
    login_save = collect_credential(
        {"credential_key": "c29_login", "display_name": "login", "purpose": "form", "store_target": "sealbox",
         "fields": [
             {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
             {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
             {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "user_input"},
         ]},
        {"username": "alice", "password": "PW-ORIG", "api_token": "TOK-ORIG"},
        scope=alice_scope, allow_expiry_change=True, allow_drop_stored_records=True, now=BASE_NOW,
    )
    assert login_save.status == "success", login_save.error
    login_before = load_catalog(scope=alice_scope).credentials["c29_login"]
    pw_rec = login_before.fields["password"].resolution_order[0].item_id
    tok_rec = login_before.fields["api_token"].resolution_order[0].item_id
    pw_value_before = sealbox.secrets[pw_rec]
    sealbox.commands.clear()

    def rotate_login(username_kind: str) -> ToolResult:
        return act("credential.collect", {"request": {
            "credential_key": "c29_login", "display_name": "login", "purpose": "p", "store_target": "sealbox",
            "fields": [
                {"name": "username", "kind": username_kind, "visibility": "metadata_visible",
                 "value_source": "agent_supplied_metadata", "value": "alice"},
                {"name": "api_token", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
                {"name": "password", "kind": "token", "visibility": "secret", "value_source": "store_generated_secret"},
            ]}})

    no_username = rotate_login("custom")
    assert no_username.status == "error"
    assert no_username.content["error_code"] == CREDENTIAL_ROTATION_USERNAME_REQUIRED
    assert no_username.content["rotated_fields"] == ["password"]
    assert sealbox_sets() == []
    assert sealbox.secrets[tok_rec] == "TOK-ORIG"
    assert sealbox.secrets[pw_rec] == pw_value_before
    assert load_catalog(scope=alice_scope).credentials["c29_login"].model_dump() == login_before.model_dump()
    with_username = rotate_login("username")
    assert with_username.status == "success", with_username.error
    assert sealbox.secrets[tok_rec] != "TOK-ORIG"

    # --- C30: the critic's capture-then-rotate run. Collect over a capture is refused and the key
    # stays open to a new capture.
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    expiry = format_utc_timestamp(now_utc + timedelta(hours=2))
    src = layout.person_dir(PERSON_ID) / "scratch" / "c30.txt"
    src.write_text("captured-v1", encoding="utf-8")
    cap = capture_file_credential(
        _valid_params(path="scratch/c30.txt", credential_key="c30_cap", expires_at=expiry, now=now_utc),
        context=ctx, access_gate=gate, now=now_utc,
    )
    assert cap.status == "success", cap.error
    cap_before = load_catalog(scope=alice_scope).credentials["c30_cap"]
    over_capture = act("credential.collect", {"request": {
        "credential_key": "c30_cap", "display_name": "c30", "purpose": "p", "store_target": "sealbox",
        "fields": [{"name": "secret", "kind": "token", "visibility": "secret",
                    "value_source": "store_generated_secret"}]}})
    assert over_capture.status == "error"
    assert over_capture.content["error_code"] == CREDENTIAL_CAPTURED_KEY_CAPTURE_ONLY
    assert over_capture.content["next_step"] == "credential.capture_file"
    assert load_catalog(scope=alice_scope).credentials["c30_cap"].model_dump() == cap_before.model_dump()
    src.write_text("captured-v2", encoding="utf-8")
    recapture = capture_file_credential(
        _valid_params(path="scratch/c30.txt", credential_key="c30_cap", expires_at=expiry, now=now_utc),
        context=ctx, access_gate=gate, now=now_utc,
    )
    assert recapture.status == "success", recapture.error
    assert resolve_secret_field("c30_cap", "secret", scope=alice_scope).value == "captured-v2"

    # --- C28 Bitwarden: the critic's item_name run, plus the stored-item and shared-item rules.
    bw = _FakeBitwarden()
    monkeypatch.setattr(credential_catalog.shutil, "which", lambda name: "/usr/bin/bw" if name == "bw" else None)
    monkeypatch.setattr(credential_catalog, "ensure_bitwarden_server_configured_from_env", lambda: None)
    monkeypatch.setattr(credential_catalog, "_run_command", bw.run)
    monkeypatch.setattr(credential_catalog, "_bitwarden_encode_item", lambda _exe, payload: json.dumps(payload))
    monkeypatch.setattr(credential_catalog, "generate_password", lambda **_kw: "GENERATED-BY-STORE")
    sealbox_resolver = credential_catalog._resolve_source_ref
    monkeypatch.setattr(
        credential_catalog,
        "_resolve_source_ref",
        lambda ref, **kw: "bw-value" if ref.source == "bitwarden" else sealbox_resolver(ref, **kw),
    )
    login_fields = [
        {"name": "username", "kind": "username", "visibility": "metadata_visible", "value_source": "user_input"},
        {"name": "password", "kind": "password", "visibility": "secret", "value_source": "user_input"},
    ]
    bank_save = collect_credential(
        {"credential_key": "bank", "display_name": "Bank", "purpose": "form", "write_source": "bitwarden",
         "item_name": "Bank Login", "fields": login_fields},
        {"username": "alice", "password": "BANK-PERSON-PW"},
        scope=alice_scope, now=BASE_NOW,
    )
    assert bank_save.status == "success", bank_save.error
    bank_before = load_catalog(scope=alice_scope).credentials["bank"]

    def bw_collect(key: str, **extra: Any) -> ToolResult:
        return act("credential.collect", {"request": {
            "credential_key": key, "display_name": key, "purpose": "p", "write_source": "bitwarden",
            "fields": [
                {"name": "username", "kind": "username", "visibility": "metadata_visible",
                 "value_source": "agent_supplied_metadata", "value": "alice"},
                {"name": "password", "kind": "password", "visibility": "secret",
                 "value_source": "store_generated_secret"},
            ], **extra}})

    named = bw_collect("other", item_name="Bank Login")
    assert named.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert named.content["rejected_param"] == "item_name"
    assert bw.items["Bank Login"]["login"]["password"] == "BANK-PERSON-PW"
    # A new key whose derived item name already exists in the shared Bitwarden session is refused.
    taken_name = "team-nats-agents :: default :: taken_key"
    bw.items[taken_name] = {"id": "bw-other-owner", "name": taken_name, "login": {"password": "OTHER-OWNER-PW"}}
    taken = bw_collect("taken_key")
    assert taken.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert bw.items[taken_name]["login"]["password"] == "OTHER-OWNER-PW"
    assert "taken_key" not in load_catalog(scope=alice_scope).credentials
    # Register refuses an item another key uses; a fresh item still registers.
    shared_reg = act(
        "credential.register_existing_source",
        {"credential_key": "bank_alias", "field_name": "password", "source": "bitwarden",
         "item_name": "Bank Login", "field_ref": "password"},
    )
    assert shared_reg.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert shared_reg.content["conflicting_credential_key"] == "bank"
    fresh_reg = act(
        "credential.register_existing_source",
        {"credential_key": "fresh_bw", "field_name": "password", "source": "bitwarden",
         "item_name": "Some Other Item", "field_ref": "password"},
    )
    assert fresh_reg.status == "success", fresh_reg.error
    # Rotating the existing key writes to its stored item, not a derived one.
    rotated_bank = bw_collect("bank")
    assert rotated_bank.status == "success", rotated_bank.error
    assert bw.items["Bank Login"]["login"]["password"] == "GENERATED-BY-STORE"
    assert "team-nats-agents :: default :: bank" not in bw.items
    bank_after = load_catalog(scope=alice_scope).credentials["bank"]
    assert bank_after.fields["password"].resolution_order[0].item_name == "Bank Login"
    assert bank_after.approval.get("recorded_by") == "agent"
    assert bank_before.approval.get("approved_by") == "user"
    # Once another key uses the same item, rotation through either key is refused.
    second = collect_credential(
        {"credential_key": "bank_twin", "display_name": "Twin", "purpose": "form", "write_source": "bitwarden",
         "item_name": "Bank Login", "fields": login_fields},
        {"username": "alice", "password": "TWIN-PW"},
        scope=alice_scope, now=BASE_NOW,
    )
    assert second.status == "success", second.error
    shared_rotate = bw_collect("bank")
    assert shared_rotate.content["error_code"] == CREDENTIAL_RECORD_NAME_NOT_ALLOWED
    assert shared_rotate.content["conflicting_credential_key"] == "bank_twin"
    assert bw.items["Bank Login"]["login"]["password"] == "TWIN-PW"
