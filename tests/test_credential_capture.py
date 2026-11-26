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
from secure_tools.providers import ToolContext
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
    # Also resolve through fake sealbox when reading via resolve_credential / transfer export.
    monkeypatch.setattr(
        credential_catalog,
        "_resolve_source_ref",
        lambda ref, **_kwargs: sealbox.secrets.get(ref.item_id or "") if ref.source == "sealbox" else None,
    )
    credential_catalog._SECRET_CACHE.clear()

    store = access_store(tmp_path / "access.db")
    gate = linked_gate(store, team_id=TEST_TEAM, instance_root=layout.root)
    return layout, sealbox, store, gate


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
    planned_item = "team-nats-agents:default:shared_target:secret"
    sealbox.secrets[planned_item] = "preexisting-record-secret"
    reg = register_existing_source(
        {
            "credential_key": "other_alias",
            "field_name": "secret",
            "source": "sealbox",
            "item_id": planned_item,
            "field_ref": "value",
        },
        scope=alice_scope,
    )
    assert reg.status == "success", reg.error
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

    expiry = BASE_NOW + timedelta(minutes=10)
    res = capture_file_credential(
        _valid_params(
            path="scratch/lifecycle.txt",
            credential_key="lifecycle_key",
            field_name="api_key",
            expires_at=format_utc_timestamp(expiry),
            now=BASE_NOW,
        ),
        context=_person_context(),
        access_gate=gate,
        now=BASE_NOW,
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
    outcomes = import_credentials(exported, team_id=TEST_TEAM, now=BASE_NOW)
    assert any(o.key.endswith("/catalog_entry/lifecycle_key") and o.imported for o in outcomes)
    imported_cred = load_catalog(scope=alice_scope).credentials["lifecycle_key"]
    assert imported_cred.expires_at == format_utc_timestamp(expiry)
    assert is_agent_captured_for_person(imported_cred, PERSON_ID)
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", orig_db)

    # Link the captured credential to an account tool, then advance past expiry.
    link = store_account_link(store, "http:tavily", "lifecycle_key", owner_id=PERSON_ID)
    assert linked_credential_expired(link, now=BASE_NOW + timedelta(minutes=5)) is False
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


def test_second_overwrite_guard_call_refuses_on_concurrent_team_key_and_keeps_file_and_catalog_row(
    env_setup,
) -> None:
    """C11: a conflicting team key created mid-write triggers the second guard call and keeps the file."""

    layout, sealbox, _store, gate = env_setup
    alice_scope = CredentialOwnerScope.user(PERSON_ID)
    team_scope = CredentialOwnerScope.team(TEST_TEAM)

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

    second_src = layout.person_dir(PERSON_ID) / "scratch" / "guard_second.txt"
    second_src.write_text("updated-secret", encoding="utf-8")
    injected = {"done": False}

    def inject_team_key_during_write(_cmd: list[str], _key: str, _val: str | None) -> None:
        if injected["done"]:
            return
        injected["done"] = True
        team_cred = CatalogCredential(
            display_name="Team Key Added Mid-Write",
            purpose="Concurrent team key",
            fields={
                "secret": CatalogCredentialField(
                    kind="token",
                    visibility="secret",
                    value_source="user_input",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_id="team-guard:secret", field_ref="value")
                    ],
                )
            },
        )
        credential_catalog._save_catalog_credential("guard_race_key", team_cred, scope=team_scope)

    sealbox.on_set_hook = inject_team_key_during_write
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
    assert injected["done"] is True
    assert second.status == "error"
    assert second.content["error_code"] == CAPTURE_TEAM_KEY_REFUSED
    assert second.content["source_file_deleted"] is False
    assert second_src.exists()
    # The person's catalog row kept its original 1-hour expiry.
    assert load_catalog(scope=alice_scope).credentials["guard_race_key"].expires_at == first_expiry


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
