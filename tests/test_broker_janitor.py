"""Tests for the broker janitor, compare-and-delete, link revocation, notices, and Sealbox tenant cleanup (A4, Feature 5)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from access_control.entities import AGENT_ROLE_SYSTEM, role_id
from access_control.identity import ContextVerifier
from access_control.people import PERSON_ROLE_ADMIN
from access_control.tool_setup_states import (
    HOW_OWN_LINK,
    HOW_TEAM_LINK,
    SOURCE_TOOL_CALL,
    STEP_ACCOUNT_LINK,
    ToolSetupStates,
)
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccountLinkStatus,
    AccountLinkV1,
    AccountMode,
    AccountSharing,
    CredentialInjectionV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    SubjectKind,
    SubjectRefV1,
    ToolDefinitionV1,
    ToolKind,
    ToolSetupState,
)
from secure_tool_broker import credential_catalog, janitor
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    delete_credential,
    load_catalog,
)
from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
    resolve_catalog_db_path,
)
from secure_tool_broker.sealbox_cli import SEALBOX_TIMEOUT_ERROR_CODE
from secure_tool_broker.sealbox_runtime import ensure_sealbox_key_pair_files
from secure_tool_broker.tool_setup_recheck import RecheckReport
from secure_tools.providers import ToolResult
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_verify_keys

TEAM_ID = "team-janitor"
PERSON_ID = "person-alice"
OTHER_PERSON_ID = "person-bob"
USER_SCOPE = CredentialOwnerScope.user(PERSON_ID)
TEAM_SCOPE = CredentialOwnerScope.team(TEAM_ID)
NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
PAST_EXPIRY = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
FUTURE_EXPIRY = (NOW + timedelta(hours=4)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _isolated_catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credential_catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(tmp_path / "sealbox_tenants"))
    monkeypatch.setenv("SEALBOX_URL", "http://127.0.0.1:18200")
    credential_catalog._SECRET_CACHE.clear()
    yield
    credential_catalog._SECRET_CACHE.clear()


def _save_catalog_entry(
    key: str,
    *,
    scope: CredentialOwnerScope,
    expires_at: str | None,
    field_name: str = "api_token",
    updated_at: str | None = None,
) -> CatalogCredential:
    ts = updated_at or "2026-11-26T00:00:00Z"
    cred = CatalogCredential(
        display_name=key,
        purpose="Test credential",
        created_at="2026-11-26T00:00:00Z",
        updated_at=ts,
        expires_at=expires_at,
        fields={
            field_name: CatalogCredentialField(
                kind="token",
                visibility="secret",
                value_source="user_input",
                resolution_order=[
                    CatalogSourceRef(
                        source="sealbox",
                        item_id=f"item-{key}:{field_name}",
                        item_name=f"item-{key}:{field_name}",
                        field_ref="value",
                    )
                ],
            )
        },
    )
    CredentialCatalogStore(resolve_catalog_db_path()).put_credential(
        scope, key, cred.model_dump(mode="json", exclude_none=True)
    )
    return cred


def _make_gate(
    tmp_path: Path,
    *,
    store: AccessRepository | None = None,
    extra_tools: tuple[ToolDefinitionV1, ...] = (),
) -> tuple[BrokerAccessGate, AccessRepository]:
    if store is None:
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        store = AccessRepository(settings)
    demo_tool = ToolDefinitionV1(
        tool_id="http:demo_api",
        kind=ToolKind.HTTP,
        display_name="Demo API",
        account=AccountMode.LINKED_CREDENTIAL,
        hosts=("api.demo.example",),
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization",
                field="api_token",
                template="Bearer {value}",
            ),
        ),
    )
    gated_tool = ToolDefinitionV1(
        tool_id="http:gated_api",
        kind=ToolKind.HTTP,
        display_name="Gated API",
        account=AccountMode.NONE,
        hosts=("gate.demo.example",),
        gate_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="X-Gate-Key",
                field="api_token",
            ),
        ),
    )
    base_catalog = CatalogSource(None).current()
    merged_tools = dict(base_catalog.tools)
    for tool in (demo_tool, gated_tool, *extra_tools):
        merged_tools[tool.tool_id] = tool
    custom_catalog = replace(base_catalog, tools=merged_tools)
    gate = BrokerAccessGate(
        catalogs=SimpleNamespace(current=lambda: custom_catalog),  # type: ignore[arg-type]
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM_ID,
        access=None,
        links=store,
    )
    return gate, store


def _make_link(
    *,
    link_id: str,
    tool_id: str,
    credential_key: str,
    owner_kind: LinkOwnerKind = LinkOwnerKind.PERSON,
    owner_id: str = PERSON_ID,
    purpose: LinkPurpose = LinkPurpose.ACCOUNT,
) -> AccountLinkV1:
    return AccountLinkV1(
        link_id=link_id,
        team_id=TEAM_ID,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        purpose=purpose,
        credential_key=credential_key,
        injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization" if purpose is LinkPurpose.ACCOUNT else "X-Gate-Key",
                field="api_token",
                template="Bearer {value}" if purpose is LinkPurpose.ACCOUNT else "{value}",
            ),
        ),
        created_by=SubjectRefV1(kind=SubjectKind.USER, id=PERSON_ID),
        created_at_utc=NOW - timedelta(hours=2),
    )


def _provision_tenant_material(
    scope: CredentialOwnerScope,
    *,
    tenant_id: str,
    material_id: str,
    token_value: str | None = "tenant-secret-token",
    create_keys: bool = True,
) -> Path:
    store = CredentialCatalogStore(resolve_catalog_db_path())
    store.put_sealbox_tenant_mapping(scope, tenant_id=tenant_id, material_id=material_id)
    directory = Path(os.environ["SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR"]) / material_id
    directory.mkdir(parents=True, exist_ok=True)
    if create_keys:
        ensure_sealbox_key_pair_files(
            public_key_file=directory / "public.pem",
            private_key_file=directory / "private.pem",
        )
    if token_value is not None:
        (directory / "token").write_text(token_value, encoding="utf-8")
    return directory


# --- 1. Timing constants and single background loop --------------------------


@pytest.mark.asyncio
async def test_janitor_constants_and_background_loop_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert janitor.INITIAL_DELAY_SECONDS == 600.0
    assert janitor.JANITOR_INTERVAL_SECONDS == 6 * 3600.0

    gate, _store = _make_gate(tmp_path)
    sleeps: list[float] = []
    runs: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError()

    def fake_run_once(_gate: Any, *, tool_setup_recheck_days: int) -> janitor.JanitorReport:
        runs.append(tool_setup_recheck_days)
        return janitor.JanitorReport()

    monkeypatch.setattr(janitor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(janitor, "run_janitor_once", fake_run_once)

    with pytest.raises(asyncio.CancelledError):
        await janitor.run_broker_janitor(
            gate,
            tool_setup_recheck_days=14,
            initial_delay_seconds=600.0,
            interval_seconds=21600.0,
        )

    assert sleeps == [600.0, 21600.0, 21600.0]
    assert runs == [14, 14]


@pytest.mark.asyncio
async def test_janitor_loop_catches_unexpected_run_exception_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gate, _store = _make_gate(tmp_path)
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError()

    def flaky_run_once(_gate: Any, *, tool_setup_recheck_days: int) -> janitor.JanitorReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected thread crash")
        return janitor.JanitorReport()

    monkeypatch.setattr(janitor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(janitor, "run_janitor_once", flaky_run_once)

    with caplog.at_level("WARNING", logger="secure_tool_broker.janitor"):
        with pytest.raises(asyncio.CancelledError):
            await janitor.run_broker_janitor(
                gate,
                tool_setup_recheck_days=14,
                initial_delay_seconds=1.0,
                interval_seconds=2.0,
            )

    assert calls == 2
    assert sleeps == [1.0, 2.0, 2.0]
    assert "event=broker_janitor_run_failed" in caplog.text


# --- 2. Store helpers and compare-and-delete concurrency ---------------------


def test_store_compare_and_delete_protects_concurrently_renewed_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialCatalogStore(resolve_catalog_db_path())
    _save_catalog_entry("renewed_key", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    row = store.load_credential(USER_SCOPE, "renewed_key")
    assert row is not None
    assert row.credential["expires_at"] == PAST_EXPIRY

    # Simulate a concurrent renewal saving a new future expiry (or clearing expiry).
    _save_catalog_entry("renewed_key", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)

    # Low-level store compare-and-delete refuses because updated_at & expires_at changed.
    assert (
        store.delete_credential_if_unchanged(
            USER_SCOPE,
            "renewed_key",
            expected_updated_at=row.updated_at,
            expected_expires_at=PAST_EXPIRY,
        )
        is False
    )
    assert store.load_credential(USER_SCOPE, "renewed_key") is not None

    # High-level delete_credential in compare-and-delete mode refuses BEFORE touching Sealbox.
    sealbox_delete_called = False

    def fail_if_sealbox_deleted(**_k: Any) -> Any:
        nonlocal sealbox_delete_called
        sealbox_delete_called = True
        raise AssertionError("Sealbox delete must not run for a renewed credential")

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fail_if_sealbox_deleted)
    result = delete_credential(
        {"credential_key": "renewed_key"},
        scope=USER_SCOPE,
        expected_updated_at=row.updated_at,
        expected_expires_at=PAST_EXPIRY,
    )
    assert result.status == "success"
    assert result.content["deleted"] is False
    assert result.content["status"] == "credential_changed"
    assert sealbox_delete_called is False
    assert load_catalog(scope=USER_SCOPE).credentials["renewed_key"].expires_at == FUTURE_EXPIRY


def test_janitor_skips_link_revocation_and_notice_when_credential_renewed_mid_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("race_key", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    link = _make_link(link_id="link-race", tool_id="http:demo_api", credential_key="race_key")
    access_store.create_link(link)

    real_delete = janitor.delete_credential

    def renew_then_delete(params: Any, **kwargs: Any) -> Any:
        # Simulate the person renewing the credential right before the janitor acquires the lock.
        _save_catalog_entry("race_key", scope=USER_SCOPE, expires_at=None)
        return real_delete(params, **kwargs)

    monkeypatch.setattr(janitor, "delete_credential", renew_then_delete)
    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.expired == 1
    assert report.deleted == 0
    assert report.skipped_changed == 1
    assert report.links_revoked == 0
    assert report.notices_sent == 0
    # Credential and active link both survived untouched.
    assert load_catalog(scope=USER_SCOPE).credentials["race_key"].expires_at is None
    assert access_store.get_link("link-race").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
    assert access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=()) == []


# --- 3. Multi-owner deletion, link revocation, system actor, notices ---------


def test_janitor_deletes_expired_credentials_revokes_links_and_sends_exact_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, access_store = _make_gate(tmp_path)
    # 1. Expired personal credential linked to private tool (no team fallback).
    _save_catalog_entry("alice_private_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    TeamAccountSharing(access_store, TEAM_ID).choose_tool(
        gate.catalog.tools["http:demo_api"], AccountSharing.PRIVATE, by="admin-1"
    )
    access_store.create_link(
        _make_link(
            link_id="link-alice-private",
            tool_id="http:demo_api",
            credential_key="alice_private_temp",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON_ID,
        )
    )
    ToolSetupStates(access_store, clock=lambda: NOW - timedelta(hours=1)).note_ready(
        TEAM_ID, PERSON_ID, "http:demo_api", how=HOW_OWN_LINK, source=SOURCE_TOOL_CALL
    )

    # 2. Expired personal credential with NO account link (must produce zero notices).
    _save_catalog_entry("alice_unlinked_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)

    # 3. Active temporary credential and lasting credential (must survive).
    _save_catalog_entry("alice_active_temp", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)
    _save_catalog_entry("alice_lasting", scope=USER_SCOPE, expires_at=None)

    # 4. Expired team gate credential (must notify admins with /link http:gated_api gate).
    _save_catalog_entry("team_gate_temp", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(
            link_id="link-team-gate",
            tool_id="http:gated_api",
            credential_key="team_gate_temp",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            purpose=LinkPurpose.GATE,
        )
    )

    # Mock Sealbox delete (include already_absent=True for one item to verify it counts as absent).
    deleted_items: list[tuple[str, str]] = []
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )

    def fake_delete_sealbox_item(*, item_key: str, item_kind: str, env: Any = None) -> dict[str, Any]:
        deleted_items.append((item_key, item_kind))
        return {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": item_key.startswith("item-alice_unlinked_temp"),
        }

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fake_delete_sealbox_item)

    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.scanned == 5
    assert report.expired == 3
    assert report.deleted == 3
    assert report.failed == 0
    assert report.links_revoked == 2
    assert report.notices_sent == 2
    assert report.link_failures == 0

    # Expired catalog entries gone; active and lasting entries kept.
    user_catalog = load_catalog(scope=USER_SCOPE).credentials
    assert set(user_catalog) == {"alice_active_temp", "alice_lasting"}
    assert load_catalog(scope=TEAM_SCOPE).credentials == {}
    assert {item for item, _kind in deleted_items} == {
        "item-alice_private_temp:api_token",
        "item-alice_unlinked_temp:api_token",
        "item-team_gate_temp:api_token",
    }

    # Verify revocation actor is the system role, NEVER a fake person.
    expected_actor = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(TEAM_ID, AGENT_ROLE_SYSTEM))
    revoked_private = access_store.get_link("link-alice-private")
    assert revoked_private is not None
    assert revoked_private.status is AccountLinkStatus.REVOKED
    assert revoked_private.revoked_by == expected_actor
    assert revoked_private.revoked_by.kind is SubjectKind.ROLE

    revoked_gate = access_store.get_link("link-team-gate")
    assert revoked_gate is not None
    assert revoked_gate.status is AccountLinkStatus.REVOKED
    assert revoked_gate.revoked_by == expected_actor

    # Personal link notice: exactly one, to Alice, states no linked account remains and exact /link command.
    alice_notices = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert len(alice_notices) == 1
    assert alice_notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
    assert alice_notices[0].recipient_person_id == PERSON_ID
    assert alice_notices[0].recipient_roles == ()
    assert "Demo API" in alice_notices[0].summary
    assert "no linked account" in alice_notices[0].summary
    assert "/link http:demo_api" in alice_notices[0].summary

    # Tool setup state flipped to needs_setup (account_link).
    setup_state = ToolSetupStates(access_store).get(TEAM_ID, PERSON_ID, "http:demo_api")
    assert setup_state is not None
    assert setup_state.state is ToolSetupState.NEEDS_SETUP
    assert setup_state.missing_step == STEP_ACCOUNT_LINK

    # Team gate link notice: sent to team administrators with /link http:gated_api gate.
    admin_notices = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=OTHER_PERSON_ID, roles=(PERSON_ROLE_ADMIN,))]
    assert len(admin_notices) == 1
    assert admin_notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
    assert admin_notices[0].recipient_person_id is None
    assert admin_notices[0].recipient_roles == (PERSON_ROLE_ADMIN,)
    assert "Gated API" in admin_notices[0].summary
    assert "/link http:gated_api gate" in admin_notices[0].summary

    # Non-admin unrelated person sees zero notices.
    assert access_store.notices_for(TEAM_ID, person_id=OTHER_PERSON_ID, roles=()) == []


def test_janitor_personal_link_notice_reports_shared_team_fallback_when_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, access_store = _make_gate(tmp_path)
    TeamAccountSharing(access_store, TEAM_ID).choose_tool(
        gate.catalog.tools["http:demo_api"], AccountSharing.SHARED, by="admin-1"
    )
    # Active team credential + team link exist; Alice's personal temporary credential expired.
    _save_catalog_entry("team_shared_lasting", scope=TEAM_SCOPE, expires_at=None)
    access_store.create_link(
        _make_link(
            link_id="link-team-shared",
            tool_id="http:demo_api",
            credential_key="team_shared_lasting",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
        )
    )
    _save_catalog_entry("alice_override_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(
            link_id="link-alice-override",
            tool_id="http:demo_api",
            credential_key="alice_override_temp",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON_ID,
        )
    )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.deleted == 1
    assert report.links_revoked == 1
    assert report.notices_sent == 1

    # Notice explicitly tells Alice that work now uses the team's shared account and gives /link http:demo_api.
    alice_notices = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert len(alice_notices) == 1
    assert "team's shared account" in alice_notices[0].summary
    assert "/link http:demo_api" in alice_notices[0].summary

    # Tool setup state records team_link fallback as ready.
    setup_state = ToolSetupStates(access_store).get(TEAM_ID, PERSON_ID, "http:demo_api")
    assert setup_state is not None
    assert setup_state.state is ToolSetupState.READY
    assert setup_state.how == HOW_TEAM_LINK


@pytest.mark.parametrize("fail_step", ["revoke_link", "create_notice_if_absent"])
def test_janitor_retains_row_and_retries_when_link_revocation_or_notice_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_step: str
) -> None:
    """C02/C22: a link revocation or notice failure leaves the catalog row so the next run finishes with 1 revoke & 1 notice."""

    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("retry_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(
            link_id="link-retry-temp",
            tool_id="http:demo_api",
            credential_key="retry_temp",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON_ID,
        )
    )
    sealbox_deletes: list[str] = []
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: (
            sealbox_deletes.append(item_key)
            or {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}
        ),
    )

    real_method = getattr(access_store, fail_step)
    attempts = 0

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(f"transient {fail_step} outage")
        return real_method(*args, **kwargs)

    monkeypatch.setattr(access_store, fail_step, fail_once)

    first = janitor.clean_expired_credentials(gate, now=NOW)
    assert first.deleted == 0
    assert first.failed == 1
    assert first.link_failures == 1
    assert sealbox_deletes == []
    # Catalog row remains for the retry. Revocation runs before notice (F9), so a
    # revoke failure leaves the link active while a notice failure revokes it first
    # and retries notice delivery on the next sweep.
    assert "retry_temp" in load_catalog(scope=USER_SCOPE).credentials
    expected_first_status = AccountLinkStatus.ACTIVE if fail_step == "revoke_link" else AccountLinkStatus.REVOKED
    assert access_store.get_link("link-retry-temp").status is expected_first_status  # type: ignore[union-attr]
    assert access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=()) == []

    second = janitor.clean_expired_credentials(gate, now=NOW)
    assert second.deleted == 1
    assert second.failed == 0
    assert second.link_failures == 0
    assert first.links_revoked + second.links_revoked == 1
    assert first.notices_sent + second.notices_sent == 1
    assert sealbox_deletes == ["item-retry_temp:api_token"]
    assert "retry_temp" not in load_catalog(scope=USER_SCOPE).credentials
    assert access_store.get_link("link-retry-temp").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    # Exactly one notice total across both runs (deterministic notice id).
    alice_notices = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert len(alice_notices) == 1
    assert "/link http:demo_api" in alice_notices[0].summary


def test_janitor_retries_sealbox_delete_without_sending_second_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C22/C25(5): a Sealbox delete error keeps the catalog row; the retry deletes it and sends no second notice."""

    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("sealbox_retry_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(
            link_id="link-sealbox-retry",
            tool_id="http:demo_api",
            credential_key="sealbox_retry_temp",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id=PERSON_ID,
        )
    )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    delete_attempts = 0

    def fail_sealbox_once(*, item_key: str, item_kind: str, env: Any = None) -> Any:
        nonlocal delete_attempts
        delete_attempts += 1
        if delete_attempts == 1:
            return ToolResult(status="error", error="sealbox temporarily unreachable")
        return {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}

    monkeypatch.setattr(credential_catalog, "delete_sealbox_item", fail_sealbox_once)

    first = janitor.clean_expired_credentials(gate, now=NOW)
    assert first.deleted == 0
    assert first.failed == 1
    assert first.links_revoked == 1
    assert first.notices_sent == 1
    assert "sealbox_retry_temp" in load_catalog(scope=USER_SCOPE).credentials

    second = janitor.clean_expired_credentials(gate, now=NOW)
    assert second.deleted == 1
    assert second.failed == 0
    assert second.links_revoked == 0
    assert second.notices_sent == 0
    assert "sealbox_retry_temp" not in load_catalog(scope=USER_SCOPE).credentials
    alice_notices = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert len(alice_notices) == 1


def test_janitor_unreadable_catalog_row_does_not_stop_sweep_for_other_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C25(2): one corrupt JSON row is counted as failed and logged without stopping other owners' cleanup."""

    import sqlite3

    gate, _access_store = _make_gate(tmp_path)
    _save_catalog_entry("bob_expired_temp", scope=CredentialOwnerScope.user(OTHER_PERSON_ID), expires_at=PAST_EXPIRY)
    # Inject a corrupt JSON row for another owner directly into SQLite.
    with sqlite3.connect(resolve_catalog_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO credential_catalog_entries
                (scope_kind, owner_id, credential_key, credential_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("user", PERSON_ID, "corrupt_row", "{not valid json", "2026-11-26T00:00:00Z", "2026-11-26T00:00:00Z"),
        )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    with caplog.at_level("WARNING", logger="secure_tool_broker.janitor"):
        report = janitor.clean_expired_credentials(gate, now=NOW)

    assert report.scanned == 2
    assert report.expired == 1
    assert report.deleted == 1
    assert report.failed == 1
    assert "bob_expired_temp" not in load_catalog(scope=CredentialOwnerScope.user(OTHER_PERSON_ID)).credentials
    assert "event=broker_janitor_credential_row_unreadable" in caplog.text
    assert "corrupt_row" in caplog.text


# --- 4. Per-tenant Sealbox cleanup & Lead Intervention 0011 env isolation ----


def test_sealbox_cleanup_isolates_tenant_env_and_never_uses_inherited_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root_token_file = tmp_path / "root-token"
    root_token_file.write_text("root-super-secret", encoding="utf-8")
    fake_home = tmp_path / "broker-home"
    fake_home.mkdir()
    (fake_home / ".config").mkdir()

    # Poison process environment with every dangerous variable named in Lead Intervention 0011.
    monkeypatch.setenv("SEALBOX_TOKEN", "inherited-raw-token")
    monkeypatch.setenv("SEALBOX_TOKEN_FILE", str(root_token_file))
    monkeypatch.setenv("SEALBOX_ROOT_TOKEN_FILE", str(root_token_file))
    monkeypatch.setenv("SEALBOX_CONFIG", str(fake_home / "sealbox.toml"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))

    dir_user = _provision_tenant_material(USER_SCOPE, tenant_id="tenant-user-1", material_id="owner-user-1")
    dir_team = _provision_tenant_material(TEAM_SCOPE, tenant_id="tenant-team-1", material_id="owner-team-1")
    # Third tenant missing its token file -> must be refused & logged without running sealbox-cli.
    _provision_tenant_material(
        CredentialOwnerScope.user(OTHER_PERSON_ID),
        tenant_id="tenant-broken-3",
        material_id="owner-broken-3",
        token_value=None,
    )

    seen_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run_sealbox(args: list[str], *, env: Any = None, timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
        seen_calls.append((list(args), dict(env or {})))
        return subprocess.CompletedProcess(
            args=["sealbox-cli", *args],
            returncode=0,
            stdout='{"deleted_count": 2}',
            stderr="",
        )

    monkeypatch.setattr(janitor, "run_sealbox", fake_run_sealbox)

    with caplog.at_level("INFO", logger="secure_tool_broker.janitor"):
        report = janitor.cleanup_expired_sealbox_tenants()

    assert report.tenants_scanned == 3
    assert report.tenants_cleaned == 2
    assert report.tenants_skipped == 1
    assert report.tenants_failed == 0
    assert report.secrets_deleted == 4
    assert report.unsupported_cli is False
    assert len(seen_calls) == 2

    expected_token_files = {str(dir_user / "token"), str(dir_team / "token")}
    seen_token_files: set[str] = set()
    for args, env in seen_calls:
        assert args == ["secret", "cleanup-expired", "--output", "json"]
        assert "SEALBOX_TOKEN" not in env
        assert "SEALBOX_ROOT_TOKEN_FILE" not in env
        assert "SEALBOX_CONFIG" not in env
        assert env["HOME"] != str(fake_home)
        assert env["XDG_CONFIG_HOME"] != str(fake_home / ".config")
        assert env["SEALBOX_TOKEN_FILE"] != str(root_token_file)
        seen_token_files.add(env["SEALBOX_TOKEN_FILE"])
    assert seen_token_files == expected_token_files

    # Logs include tenant mapping ids for both cleaned and skipped tenants.
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "tenant-user-1" in log_text
    assert "tenant-team-1" in log_text
    assert "tenant-broken-3" in log_text


def test_sealbox_cleanup_warns_once_on_older_cli_without_subcommand(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _provision_tenant_material(USER_SCOPE, tenant_id="tenant-a", material_id="owner-a")
    _provision_tenant_material(TEAM_SCOPE, tenant_id="tenant-b", material_id="owner-b")

    calls = 0

    def fake_run_sealbox(args: list[str], *, env: Any = None, timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=["sealbox-cli", *args],
            returncode=2,
            stdout="",
            stderr="error: unrecognized subcommand 'cleanup-expired'",
        )

    monkeypatch.setattr(janitor, "run_sealbox", fake_run_sealbox)

    with caplog.at_level("WARNING", logger="secure_tool_broker.janitor"):
        report = janitor.cleanup_expired_sealbox_tenants()

    assert calls == 1
    assert report.unsupported_cli is True
    assert report.tenants_cleaned == 0
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "sealbox-cli secret cleanup-expired" in warnings[0]


def test_sealbox_cleanup_timeout_uses_90s_and_logs_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C23 / Lead Intervention 0006: cleanup timeout > 70s and timeouts log as unknown outcome."""

    assert janitor.SEALBOX_CLEANUP_TIMEOUT_SECONDS > 70
    _provision_tenant_material(USER_SCOPE, tenant_id="tenant-slow", material_id="owner-slow")
    _provision_tenant_material(TEAM_SCOPE, tenant_id="tenant-fast", material_id="owner-fast")

    seen_timeouts: list[int] = []
    call_index = 0

    def fake_run_sealbox(args: list[str], *, env: Any = None, timeout_seconds: int = 30) -> Any:
        nonlocal call_index
        seen_timeouts.append(timeout_seconds)
        call_index += 1
        if call_index == 1:
            return ToolResult(
                status="error",
                error=f"sealbox-cli command timed out after {timeout_seconds}s",
                content={"error_code": SEALBOX_TIMEOUT_ERROR_CODE, "timeout_seconds": timeout_seconds},
            )
        return subprocess.CompletedProcess(
            args=["sealbox-cli", *args],
            returncode=0,
            stdout='{"deleted_count": 1, "cleaned_at": "2026-11-26T12:00:00Z"}',
            stderr="",
        )

    monkeypatch.setattr(janitor, "run_sealbox", fake_run_sealbox)

    with caplog.at_level("WARNING", logger="secure_tool_broker.janitor"):
        report = janitor.cleanup_expired_sealbox_tenants()

    assert seen_timeouts == [janitor.SEALBOX_CLEANUP_TIMEOUT_SECONDS, janitor.SEALBOX_CLEANUP_TIMEOUT_SECONDS]
    assert report.tenants_scanned == 2
    assert report.tenants_timed_out == 1
    assert report.tenants_cleaned == 1
    assert report.tenants_failed == 0
    assert report.secrets_deleted == 1
    assert "event=broker_janitor_sealbox_tenant_timeout" in caplog.text
    assert "outcome=unknown" in caplog.text


def test_sealbox_cleanup_handles_exit_error_missing_count_and_continues_next_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C25(4)+(5): non-zero exit does not stop later tenants, and missing deleted_count logs a warning."""

    _provision_tenant_material(TEAM_SCOPE, tenant_id="tenant-1-err", material_id="owner-1-err")
    _provision_tenant_material(USER_SCOPE, tenant_id="tenant-2-nocount", material_id="owner-2-nocount")
    _provision_tenant_material(
        CredentialOwnerScope.user(OTHER_PERSON_ID),
        tenant_id="tenant-3-ok",
        material_id="owner-3-ok",
    )

    responses = [
        subprocess.CompletedProcess(args=["sealbox-cli"], returncode=1, stdout="", stderr="connection refused"),
        subprocess.CompletedProcess(args=["sealbox-cli"], returncode=0, stdout='{"cleaned_at": "2026-11-26T12:00:00Z"}', stderr=""),
        subprocess.CompletedProcess(args=["sealbox-cli"], returncode=0, stdout='{"deleted_count": 3, "cleaned_at": "2026-11-26T12:00:00Z"}', stderr=""),
    ]
    index = 0

    def fake_run_sealbox(args: list[str], *, env: Any = None, timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
        nonlocal index
        result = responses[index]
        index += 1
        return result

    monkeypatch.setattr(janitor, "run_sealbox", fake_run_sealbox)

    with caplog.at_level("WARNING", logger="secure_tool_broker.janitor"):
        report = janitor.cleanup_expired_sealbox_tenants()

    assert report.tenants_scanned == 3
    assert report.tenants_failed == 1
    assert report.tenants_cleaned == 2
    assert report.secrets_deleted == 3
    assert "event=broker_janitor_sealbox_tenant_failed" in caplog.text
    assert "event=broker_janitor_sealbox_cleanup_count_missing" in caplog.text


# --- 5. Job isolation & tool_setup_recheck integration -----------------------


def test_run_janitor_once_isolates_failing_jobs_and_respects_recheck_days_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, _store = _make_gate(tmp_path)
    called_jobs: list[str] = []

    def boom_credentials(_gate: Any, *, now: Any = None) -> Any:
        called_jobs.append("credentials")
        raise RuntimeError("catalog db busy")

    def ok_sealbox() -> janitor.SealboxCleanupReport:
        called_jobs.append("sealbox")
        return janitor.SealboxCleanupReport(tenants_scanned=1, tenants_cleaned=1, secrets_deleted=3)

    def ok_recheck(_gate: Any, *, every_days: int, now: Any = None) -> RecheckReport:
        called_jobs.append(f"recheck:{every_days}")
        return RecheckReport(checked=0 if every_days == 0 else 2, still_ready=0 if every_days == 0 else 2)

    monkeypatch.setattr(janitor, "clean_expired_credentials", boom_credentials)
    monkeypatch.setattr(janitor, "cleanup_expired_sealbox_tenants", ok_sealbox)
    monkeypatch.setattr(janitor, "recheck_due", ok_recheck)

    report_off = janitor.run_janitor_once(gate, tool_setup_recheck_days=0, now=NOW)
    assert called_jobs == ["credentials", "sealbox", "recheck:0"]
    assert report_off.job_errors == ("expired_credentials",)
    assert report_off.sealbox_cleanup.tenants_cleaned == 1
    assert report_off.tool_setup_recheck.checked == 0

    called_jobs.clear()
    report_on = janitor.run_janitor_once(gate, tool_setup_recheck_days=30, now=NOW)
    assert called_jobs == ["credentials", "sealbox", "recheck:30"]
    assert report_on.tool_setup_recheck.checked == 2


# --- 6. Real PostgreSQL backend verification for link revocation & notices ---


@pytest.mark.live
@pytest.mark.postgres
@pytest.mark.integration
def test_janitor_link_revocation_and_notices_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_access_store_postgres import postgres_storage

    with postgres_storage() as pg_settings:
        pg_store = AccessRepository(pg_settings)
        gate, _ = _make_gate(tmp_path, store=pg_store)
        _save_catalog_entry("pg_temp_cred", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
        _save_catalog_entry("pg_team_cred", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
        pg_store.create_link(
            _make_link(
                link_id="pg-link-user",
                tool_id="http:demo_api",
                credential_key="pg_temp_cred",
                owner_kind=LinkOwnerKind.PERSON,
                owner_id=PERSON_ID,
            )
        )
        pg_store.create_link(
            _make_link(
                link_id="pg-link-team",
                tool_id="http:gated_api",
                credential_key="pg_team_cred",
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=TEAM_ID,
                purpose=LinkPurpose.GATE,
            )
        )
        monkeypatch.setattr(
            credential_catalog,
            "sealbox_tenant_environment",
            lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
        )
        monkeypatch.setattr(
            credential_catalog,
            "delete_sealbox_item",
            lambda *, item_key, item_kind, env=None: {
                "source": "sealbox",
                "item_key": item_key,
                "item_kind": item_kind,
                "already_absent": False,
            },
        )

        report = janitor.clean_expired_credentials(gate, now=NOW)
        assert report.deleted == 2
        assert report.links_revoked == 2
        assert report.notices_sent == 2

        expected_actor = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(TEAM_ID, AGENT_ROLE_SYSTEM))
        assert pg_store.get_link("pg-link-user").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
        assert pg_store.get_link("pg-link-user").revoked_by == expected_actor  # type: ignore[union-attr]
        assert pg_store.get_link("pg-link-team").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
        assert pg_store.get_link("pg-link-team").revoked_by == expected_actor  # type: ignore[union-attr]

        user_notices = [n for n, _read in pg_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
        admin_notices = [n for n, _read in pg_store.notices_for(TEAM_ID, person_id=OTHER_PERSON_ID, roles=(PERSON_ROLE_ADMIN,))]
        assert len(user_notices) == 1
        assert user_notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
        assert "/link http:demo_api" in user_notices[0].summary
        assert len(admin_notices) == 1
        assert admin_notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
        assert "/link http:gated_api gate" in admin_notices[0].summary


# --- 7. Project review F8, F9, F12, F16, F19 tests ---------------------------


def test_f8_janitor_reloads_and_retries_when_row_rewritten_still_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: if a row is rewritten mid-sweep with a still-expired expiry, janitor reloads and deletes it."""

    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("rewritten_expired", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(link_id="link-rewritten", tool_id="http:demo_api", credential_key="rewritten_expired")
    )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    real_delete = janitor.delete_credential
    rewrote = False

    def rewrite_once_then_delete(params: Any, **kwargs: Any) -> Any:
        nonlocal rewrote
        if not rewrote:
            rewrote = True
            # Rewrite the row (new updated_at) while keeping it still expired.
            _save_catalog_entry(
                "rewritten_expired",
                scope=USER_SCOPE,
                expires_at=PAST_EXPIRY,
                updated_at="2026-11-26T11:59:00Z",
            )
        return real_delete(params, **kwargs)

    monkeypatch.setattr(janitor, "delete_credential", rewrite_once_then_delete)
    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.deleted == 1
    assert report.skipped_changed == 0
    assert report.links_revoked == 1
    assert report.notices_sent == 1
    assert "rewritten_expired" not in load_catalog(scope=USER_SCOPE).credentials
    assert access_store.get_link("link-rewritten").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]


def test_f9_relinking_during_janitor_link_step_sends_no_false_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9: if the person relinks between list_links and revoke_link, janitor sends no notice and preserves setup state."""

    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("relinked_old", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    _save_catalog_entry("relinked_fresh", scope=USER_SCOPE, expires_at=None)
    access_store.create_link(
        _make_link(link_id="link-old", tool_id="http:demo_api", credential_key="relinked_old")
    )
    ToolSetupStates(access_store, clock=lambda: NOW - timedelta(minutes=5)).note_ready(
        TEAM_ID, PERSON_ID, "http:demo_api", how=HOW_OWN_LINK, source=SOURCE_TOOL_CALL
    )
    real_revoke = access_store.revoke_link

    def relink_before_revoke(link_id: str, **kwargs: Any) -> Any:
        if link_id == "link-old":
            access_store.create_link(
                _make_link(link_id="link-fresh", tool_id="http:demo_api", credential_key="relinked_fresh")
            )
        return real_revoke(link_id, **kwargs)

    monkeypatch.setattr(access_store, "revoke_link", relink_before_revoke)
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.deleted == 1
    assert report.links_revoked == 0
    assert report.notices_sent == 0
    assert access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=()) == []
    assert access_store.get_link("link-fresh").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
    setup_state = ToolSetupStates(access_store).get(TEAM_ID, PERSON_ID, "http:demo_api")
    assert setup_state is not None and setup_state.state is ToolSetupState.READY


def test_f12_notice_id_includes_expiry_so_later_expiry_is_not_suppressed(tmp_path: Path) -> None:
    """F12: distinct expires_at values for the same link produce distinct notice IDs."""

    gate, _store = _make_gate(tmp_path)
    link = _make_link(link_id="link-repeat", tool_id="http:demo_api", credential_key="repeat_key")
    first_expiry = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    second_expiry = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    id_first = janitor._expired_link_notice_id(TEAM_ID, link, expires_at=first_expiry)
    id_first_retry = janitor._expired_link_notice_id(TEAM_ID, link, expires_at=first_expiry)
    id_second = janitor._expired_link_notice_id(TEAM_ID, link, expires_at=second_expiry)

    assert id_first == id_first_retry
    assert id_first != id_second


def test_f16_notice_does_not_claim_team_fallback_when_team_credential_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F16: if the team link's credential is absent from the catalog, the personal expiry notice says no linked account."""

    gate, access_store = _make_gate(tmp_path)
    TeamAccountSharing(access_store, TEAM_ID).choose_tool(
        gate.catalog.tools["http:demo_api"], AccountSharing.SHARED, by="admin-1"
    )
    # Team link exists in access store, but its credential was NOT imported into the catalog.
    access_store.create_link(
        _make_link(
            link_id="link-team-unbacked",
            tool_id="http:demo_api",
            credential_key="missing_team_cred",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
        )
    )
    _save_catalog_entry("alice_temp", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(link_id="link-alice", tool_id="http:demo_api", credential_key="alice_temp")
    )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    report = janitor.clean_expired_credentials(gate, now=NOW)
    assert report.deleted == 1
    assert report.notices_sent == 1
    [notice] = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert "no linked account" in notice.summary
    assert "team's shared account" not in notice.summary


def test_f19_team_account_link_notice_and_private_team_link_personal_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: team ACCOUNT link expiry notifies admins with /link ... team; private team link does not count as personal fallback."""

    gate, access_store = _make_gate(tmp_path)
    TeamAccountSharing(access_store, TEAM_ID).choose_tool(
        gate.catalog.tools["http:demo_api"], AccountSharing.PRIVATE, by="admin-1"
    )
    # Valid team credential + team link exist, but tool is PRIVATE -> Alice's expiry notice must say no linked account.
    _save_catalog_entry("team_private_cred", scope=TEAM_SCOPE, expires_at=None)
    access_store.create_link(
        _make_link(
            link_id="link-team-private",
            tool_id="http:demo_api",
            credential_key="team_private_cred",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            purpose=LinkPurpose.ACCOUNT,
        )
    )
    _save_catalog_entry("alice_private_only", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(link_id="link-alice-priv", tool_id="http:demo_api", credential_key="alice_private_only")
    )

    # Also test an expired team ACCOUNT link on a second tool -> notifies admins with /link ... team.
    second_tool = ToolDefinitionV1(
        tool_id="http:team_acct_api",
        kind=ToolKind.HTTP,
        display_name="Team Account API",
        account=AccountMode.LINKED_CREDENTIAL,
        hosts=("team-acct.demo.example",),
        credential_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="api_token"),
        ),
    )
    gate2, _ = _make_gate(tmp_path, store=access_store, extra_tools=(second_tool,))
    _save_catalog_entry("team_acct_expired", scope=TEAM_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(
            link_id="link-team-acct",
            tool_id="http:team_acct_api",
            credential_key="team_acct_expired",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEAM_ID,
            purpose=LinkPurpose.ACCOUNT,
        )
    )

    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": False,
        },
    )

    report = janitor.clean_expired_credentials(gate2, now=NOW)
    assert report.deleted == 2
    assert report.links_revoked == 2
    assert report.notices_sent == 2

    [alice_notice] = [n for n, _read in access_store.notices_for(TEAM_ID, person_id=PERSON_ID, roles=())]
    assert "no linked account" in alice_notice.summary
    assert "team's shared account" not in alice_notice.summary

    [admin_notice] = [
        n for n, _read in access_store.notices_for(TEAM_ID, person_id=OTHER_PERSON_ID, roles=(PERSON_ROLE_ADMIN,))
    ]
    assert admin_notice.kind is AccessNoticeKind.CREDENTIAL_EXPIRED
    assert admin_notice.recipient_roles == (PERSON_ROLE_ADMIN,)
    assert "/link http:team_acct_api team" in admin_notice.summary


def test_f19_renewal_while_key_lock_held_survives_concurrent_janitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: real thread concurrency — holding credential_key_lock during renewal prevents janitor deletion."""

    import threading

    gate, access_store = _make_gate(tmp_path)
    _save_catalog_entry("lock_race_key", scope=USER_SCOPE, expires_at=PAST_EXPIRY)
    access_store.create_link(
        _make_link(link_id="link-lock-race", tool_id="http:demo_api", credential_key="lock_race_key")
    )
    sealbox_deleted: list[str] = []
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_k: {"SEALBOX_TOKEN_FILE": "/tmp/fake-token"},
    )
    monkeypatch.setattr(
        credential_catalog,
        "delete_sealbox_item",
        lambda *, item_key, item_kind, env=None: (
            sealbox_deleted.append(item_key)
            or {"source": "sealbox", "item_key": item_key, "item_kind": item_kind, "already_absent": False}
        ),
    )

    janitor_report_holder: list[janitor.ExpiredCredentialsReport] = []
    janitor_started = threading.Event()
    real_delete = janitor.delete_credential

    def signal_then_delete(params: Any, **kwargs: Any) -> Any:
        janitor_started.set()
        return real_delete(params, **kwargs)

    monkeypatch.setattr(janitor, "delete_credential", signal_then_delete)

    lock = credential_catalog.credential_key_lock(USER_SCOPE, "lock_race_key")
    with lock:
        worker = threading.Thread(
            target=lambda: janitor_report_holder.append(janitor.clean_expired_credentials(gate, now=NOW))
        )
        worker.start()
        assert janitor_started.wait(timeout=5.0)
        # Complete the renewal (extending expiry into the future) before releasing the lock.
        _save_catalog_entry("lock_race_key", scope=USER_SCOPE, expires_at=FUTURE_EXPIRY)

    worker.join(timeout=5.0)
    assert not worker.is_alive()
    [report] = janitor_report_holder
    assert report.deleted == 0
    assert report.skipped_changed == 1
    assert report.links_revoked == 0
    assert report.notices_sent == 0
    assert sealbox_deleted == []
    assert load_catalog(scope=USER_SCOPE).credentials["lock_race_key"].expires_at == FUTURE_EXPIRY
    assert access_store.get_link("link-lock-race").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
