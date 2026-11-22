"""Gateway Settings > Tasks > Brokered SSH service and HTTP route tests.

Covers real Cedar authorization through ``PersonChangeCheck``, SQLite
``ExecutionControlRepository`` storage, broker revocation notifications via
``HttpGatewaySecureBroker`` / ``register_execution_ssh_app``, structured
``event=`` audit logs, and ``InternalWebsiteInteractiveApp`` HTTP routes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.access_setup import MembershipRoles, PersonChangeCheck
from assistant_gateway.application.execution_ssh_page import (
    ExecutionSSHNotFound,
    ExecutionSSHRefused,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.execution_ssh import GatewayExecutionSSH
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.store import GatewayStateStore
from custom_agents.execution_changes import (
    SSH_INTERACTION_CHANGE_KIND,
    SSH_TARGET_ENROLL_CHANGE_KIND,
)
from custom_messages.contracts.access_v2 import (
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
)
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    ActivationRecordV2,
    EnrolledSSHTargetV1,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    SSHDelegationBoundsV1,
    SSHTargetLifecycle,
    SSHTargetPlatform,
    SSHTargetStatus,
    enrolled_ssh_target_digest,
)
from secure_tool_broker.defaults import EXECUTION_SSH_ROUTE_PREFIX
from secure_tool_broker.execution_ssh import register_execution_ssh_app
from storage.execution_control_repository import (
    ExecutionControlRepository,
    create_execution_control_repository,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = REPO_ROOT / "target" / "debug" / "ua-cedar-authorizer"


def _sample_ed25519_key(seed: str = "gateway-test-host") -> tuple[str, str, str]:
    key_type = b"ssh-ed25519"
    key_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
    blob = (
        len(key_type).to_bytes(4, "big")
        + key_type
        + len(key_bytes).to_bytes(4, "big")
        + key_bytes
    )
    encoded = base64.b64encode(blob).decode("ascii")
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return "ssh-ed25519", f"SHA256:{digest}", f"ssh-ed25519 {encoded}"


def _make_config(
    tmp_path: Path,
    *,
    authorization_enabled: bool = True,
    ssh_enabled: bool = True,
) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="ssh-gateway-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_ssh",
        timezone="UTC",
    )
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.authorization.enabled = authorization_enabled
    if authorization_enabled:
        assert CEDAR_BINARY.exists(), f"Cedar binary required at {CEDAR_BINARY}"
        config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    config.execution_control.enabled = True
    config.execution_control.ssh.enabled = ssh_enabled
    config.save(config.layout.instance_config_path)
    return config


def _make_person_changes(
    config: GatewayInstanceConfig,
    *,
    roles_by_person: dict[str, tuple[str, ...]],
) -> tuple[GatewayStateStore, PersonChangeCheck]:
    store = GatewayStateStore(
        config.state_storage_settings.sqlite_path or str(config.layout.gateway_state_db_path),
        storage_settings=config.state_storage_settings,
    )
    for person_id, roles in roles_by_person.items():
        store.identity_repository.create_gateway_user(
            canonical_user_id=person_id,
            display_name=person_id,
            timezone="UTC",
        )
        for role in roles:
            store.identity_repository.create_team_membership(
                team_id=config.manager.team_id,
                canonical_user_id=person_id,
                role=role,
                status="active",
            )
    return store, PersonChangeCheck(config, MembershipRoles(store.identity_repository))


def _seed_target_and_session(
    repo: ExecutionControlRepository,
    *,
    team_id: str,
    now: datetime,
    target_id: str = "ssh_tgt_01",
    session_id: str = "ssh_sess_01",
    target_expires_at: datetime | None = None,
    session_status: InteractiveSessionStatus = InteractiveSessionStatus.OPEN,
    session_expires_at: datetime | None = None,
    delegated_agent_type: str | None = "InfraWorker",
    delegated_task_id: str | None = "task_123",
    allow_broker_sudo: bool = True,
    allow_workspace_transfer: bool = True,
) -> tuple[EnrolledSSHTargetV1, InteractiveSessionV1]:
    alg, fingerprint, pubkey = _sample_ed25519_key(target_id)
    _, cred_fp, _ = _sample_ed25519_key(f"cred:{target_id}")
    owner = OwnerScopeV1(
        team_id=team_id,
        scope_kind=OwnerScopeKind.TEAM,
        owner_id=team_id,
    )
    holder = AccessHolderV1(
        agent_type="TeamArchitect",
        agent_id="agent_lead_1",
        person_id="usr_admin",
    )
    lifecycle = (
        SSHTargetLifecycle.TEMPORARY
        if target_expires_at is not None
        else SSHTargetLifecycle.PERSISTENT
    )
    target_digest = enrolled_ssh_target_digest(
        target_id=target_id,
        owner=owner,
        host="198.51.100.25",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_key_algorithm=alg,
        host_key_fingerprint=fingerprint,
        host_key_public_key=pubkey,
        credential_key="prod-ed25519",
        credential_owner_scope=owner,
        credential_public_key_fingerprint=cred_fp,
        bootstrap_credential_key="prod-bootstrap",
        bootstrap_credential_owner_scope=owner,
        lifecycle=lifecycle,
        expires_at_utc=target_expires_at,
        reason="Bootstrap Linux worker",
    )
    target_activation = ActivationRecordV2.build(
        activation_id=f"act_{target_id}",
        kind=ExecutionActivationKind.SSH_TARGET_ENROLL,
        resource_id=target_id,
        resource_digest=target_digest,
        generation=1,
        owner=owner,
        operation="fn:execution_control#change:execution_ssh_target_enroll",
        holder=holder,
        approval=ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.PERSON_APPROVAL,
            access_request_id=f"req_{target_id}",
            approved_by="usr_admin",
            approved_at_utc=now - timedelta(minutes=10),
        ),
        activated_at_utc=now - timedelta(minutes=10),
        expires_at_utc=target_expires_at,
    )
    target = EnrolledSSHTargetV1(
        target_id=target_id,
        owner=owner,
        host="198.51.100.25",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_key_algorithm=alg,
        host_key_fingerprint=fingerprint,
        host_key_public_key=pubkey,
        credential_key="prod-ed25519",
        credential_owner_scope=owner,
        credential_public_key_fingerprint=cred_fp,
        bootstrap_credential_key="prod-bootstrap",
        bootstrap_credential_owner_scope=owner,
        lifecycle=lifecycle,
        expires_at_utc=target_expires_at,
        status=SSHTargetStatus.ACTIVE,
        reason="Bootstrap Linux worker",
        activation=target_activation,
        created_at_utc=now - timedelta(minutes=10),
        updated_at_utc=now - timedelta(minutes=10),
    )
    repo.put_ssh_target(target)

    conn_target = target.to_connection_target()
    connect_activation = ActivationRecordV2.build(
        activation_id=f"act_conn_{session_id}",
        kind=ExecutionActivationKind.SSH_CONNECT,
        resource_id=session_id,
        resource_digest=conn_target.calculate_digest(),
        generation=1,
        owner=owner,
        operation="fn:execution_control#create_execution_ssh_session",
        holder=holder,
        approval=ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.HELD_ACCESS,
            approved_by="usr_admin",
            approved_at_utc=now - timedelta(minutes=5),
        ),
        activated_at_utc=now - timedelta(minutes=5),
    )
    session = InteractiveSessionV1(
        session_id=session_id,
        owner=owner,
        target=conn_target,
        target_digest=conn_target.calculate_digest(),
        connect_activation=connect_activation,
        status=session_status,
        created_at_utc=now - timedelta(minutes=5),
        updated_at_utc=now - timedelta(minutes=1),
    )
    repo.create_interactive_session(session)

    bounds = SSHDelegationBoundsV1(
        delegated_agent_type=delegated_agent_type,
        delegated_task_id=delegated_task_id,
        allow_typing=False,
        allow_password_bootstrap=False,
        allow_broker_sudo=allow_broker_sudo,
        allow_workspace_transfer=allow_workspace_transfer,
    )
    interaction_activation = ActivationRecordV2.build(
        activation_id=f"act_{session_id}",
        kind=ExecutionActivationKind.SSH_INTERACT,
        resource_id=session_id,
        resource_digest=conn_target.calculate_digest(),
        generation=1,
        owner=owner,
        operation="fn:execution_control#change:execution_ssh_interaction",
        holder=holder,
        approval=ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.PERSON_APPROVAL,
            access_request_id=f"req_{session_id}",
            approved_by="usr_admin",
            approved_at_utc=now - timedelta(minutes=4),
        ),
        activated_at_utc=now - timedelta(minutes=4),
        expires_at_utc=session_expires_at or (now + timedelta(hours=1)),
    )
    repo.put_ssh_interaction(interaction_activation, bounds=bounds)
    return target, session


class _RecordingBroker:
    def __init__(self, *, available: bool = True, fail: bool = False, terminated_live: bool = True) -> None:
        self.available = available
        self.fail = fail
        self.terminated_live = terminated_live
        self.revoked_targets: list[tuple[str, str]] = []
        self.revoked_sessions: list[tuple[str, str]] = []

    async def revoke_execution_ssh_target(self, *, target_id: str, revoked_by: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("broker offline")
        self.revoked_targets.append((target_id, revoked_by))
        return {"revoked": True, "target_id": target_id}

    async def revoke_execution_ssh_session(self, *, session_id: str, revoked_by: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("broker offline")
        self.revoked_sessions.append((session_id, revoked_by))
        return {
            "revoked": True,
            "session_id": session_id,
            "terminated_live": self.terminated_live,
        }


@pytest.mark.asyncio
async def test_gateway_execution_ssh_real_cedar_admin_vs_member_and_revocation_audit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)
    config = _make_config(tmp_path, authorization_enabled=True, ssh_enabled=True)
    _, person_changes = _make_person_changes(
        config,
        roles_by_person={
            "usr_admin": ("admin",),
            "usr_member": ("member",),
        },
    )
    repo = create_execution_control_repository(config.manager_storage_settings)
    _seed_target_and_session(
        repo,
        team_id=config.manager.team_id,
        now=now,
        target_id="ssh_tgt_01",
        session_id="ssh_sess_01",
    )
    broker = _RecordingBroker(terminated_live=True)
    service = GatewayExecutionSSH(
        team_id=config.manager.team_id,
        config_path=config.config_path,
        repository=repo,
        person_changes=person_changes,
        broker=broker,
        default_ssh_enabled=True,
        clock=lambda: now,
    )
    caplog.set_level(logging.INFO)
    try:
        # Non-admin member cannot see any hosts or sessions and cannot revoke.
        member_page = service.page(person_id="usr_member")
        assert member_page.can_manage is False
        assert member_page.targets == ()
        assert member_page.sessions == ()

        with pytest.raises(ExecutionSSHRefused):
            await service.revoke_session(person_id="usr_member", session_id="ssh_sess_01")
        with pytest.raises(ExecutionSSHRefused):
            await service.revoke_target(person_id="usr_member", target_id="ssh_tgt_01")
        assert "event=gateway_ssh_session_revoke_refused" in caplog.text
        assert "event=gateway_ssh_target_revoke_refused" in caplog.text
        assert repo.get_interactive_session_revocation(team_id="team_ssh", session_id="ssh_sess_01") is None
        assert repo.get_ssh_target(team_id="team_ssh", target_id="ssh_tgt_01").status is SSHTargetStatus.ACTIVE

        # Admin sees non-secret target & delegated session metadata.
        admin_page = service.page(person_id="usr_admin")
        assert admin_page.can_manage is True
        assert admin_page.ssh_enabled is True
        assert len(admin_page.targets) == 1
        assert admin_page.targets[0].target_id == "ssh_tgt_01"
        assert admin_page.targets[0].credential_key == "prod-ed25519"
        assert admin_page.targets[0].can_revoke is True
        assert len(admin_page.sessions) == 1
        assert admin_page.sessions[0].session_id == "ssh_sess_01"
        assert admin_page.sessions[0].delegated_agent_type == "InfraWorker"
        assert admin_page.sessions[0].delegated_task_id == "task_123"
        assert admin_page.sessions[0].capabilities == ("broker_sudo", "workspace_transfer")
        assert admin_page.sessions[0].is_active is True

        # Admin terminates the active session.
        session_result = await service.revoke_session(person_id="usr_admin", session_id="ssh_sess_01")
        assert session_result.revoked is True
        assert session_result.broker_notified is True
        assert session_result.terminated_live is True
        assert broker.revoked_sessions == [("ssh_sess_01", "usr_admin")]
        assert "event=gateway_ssh_session_revoked" in caplog.text
        assert session_result.page.sessions[0].status == "revoked"
        assert session_result.page.sessions[0].can_revoke is False

        # Admin revokes the enrolled target.
        target_result = await service.revoke_target(person_id="usr_admin", target_id="ssh_tgt_01")
        assert target_result.revoked is True
        assert target_result.broker_notified is True
        assert broker.revoked_targets == [("ssh_tgt_01", "usr_admin")]
        assert "event=gateway_ssh_target_revoked" in caplog.text
        assert target_result.page.targets[0].status == "revoked"
        assert target_result.page.targets[0].can_revoke is False

        # Unknown ids raise ExecutionSSHNotFound.
        with pytest.raises(ExecutionSSHNotFound):
            await service.revoke_target(person_id="usr_admin", target_id="ssh_tgt_missing")
        with pytest.raises(ExecutionSSHNotFound):
            await service.revoke_session(person_id="usr_admin", session_id="ssh_sess_missing")
    finally:
        person_changes.close()


@pytest.mark.asyncio
async def test_gateway_execution_ssh_broker_failure_still_persists_revocation_fail_closed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)
    config = _make_config(tmp_path, authorization_enabled=False, ssh_enabled=True)
    _, person_changes = _make_person_changes(config, roles_by_person={"usr_solo": ("member",)})
    repo = create_execution_control_repository(config.manager_storage_settings)
    _seed_target_and_session(repo, team_id=config.manager.team_id, now=now)
    service = GatewayExecutionSSH(
        team_id=config.manager.team_id,
        config_path=config.config_path,
        repository=repo,
        person_changes=person_changes,
        broker=_RecordingBroker(fail=True),
        default_ssh_enabled=True,
        clock=lambda: now,
    )
    caplog.set_level(logging.WARNING)
    try:
        # With authorization disabled (D8), active team member can revoke;
        # broker outage still persists DB revocation fail-closed.
        result = await service.revoke_target(person_id="usr_solo", target_id="ssh_tgt_01")
        assert result.revoked is True
        assert result.broker_notified is False
        assert "could not be reached right now" in result.message
        assert "event=gateway_ssh_broker_revoke_notify_failed" in caplog.text
        assert repo.get_ssh_target(team_id="team_ssh", target_id="ssh_tgt_01").status is SSHTargetStatus.REVOKED
        assert repo.get_interactive_session_revocation(team_id="team_ssh", session_id="ssh_sess_01") is not None
    finally:
        person_changes.close()


@pytest.mark.asyncio
async def test_website_execution_ssh_http_routes_enforce_csrf_and_admin_checks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_gateway_website_app import (
        InternalWebsiteInteractiveApp,
        _UserTokenStatefulFakeGateway,
    )

    now = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)
    config = _make_config(tmp_path, authorization_enabled=True, ssh_enabled=True)
    store, person_changes = _make_person_changes(
        config,
        roles_by_person={"user_alpha": ("member",)},
    )
    repo = create_execution_control_repository(config.manager_storage_settings)
    _seed_target_and_session(repo, team_id=config.manager.team_id, now=now)
    broker = _RecordingBroker(terminated_live=True)

    gateway = _UserTokenStatefulFakeGateway(store)
    gateway.execution_ssh = GatewayExecutionSSH(
        team_id=config.manager.team_id,
        config_path=config.config_path,
        repository=repo,
        person_changes=person_changes,
        broker=broker,
        default_ssh_enabled=True,
        clock=lambda: now,
    )
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    caplog.set_level(logging.INFO)
    try:
        async with TestClient(TestServer(app.build_app())) as client:
            # Asset is served cleanly by the website app.
            asset_resp = await client.get("/execution_ssh_controls.mjs")
            assert asset_resp.status == 200
            assert "createExecutionSSHControls" in await asset_resp.text()

            auth_response = await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})
            auth_payload = await auth_response.json()
            read_headers = {
                "Cookie": f"ua_web_chat_session={auth_response.cookies['ua_web_chat_session'].value}"
            }
            write_headers = {
                **read_headers,
                "Origin": str(client.make_url("/")).rstrip("/"),
                "Sec-Fetch-Site": "same-origin",
                "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
            }

            # Member gets empty view and 403 on revoke routes.
            member_view = await (await client.get("/api/web-chat/execution-ssh", headers=read_headers)).json()
            assert member_view == {
                "status": "ok",
                "can_manage": False,
                "ssh_enabled": False,
                "targets": [],
                "sessions": [],
            }
            denied_target = await client.post(
                "/api/web-chat/execution-ssh/targets/ssh_tgt_01/revoke",
                headers=write_headers,
                json={},
            )
            assert denied_target.status == 403
            denied_session = await client.post(
                "/api/web-chat/execution-ssh/sessions/ssh_sess_01/revoke",
                headers=write_headers,
                json={},
            )
            assert denied_session.status == 403

            # Promote user_alpha to admin in gateway memberships (read live by PersonChangeCheck).
            store.identity_repository.change_team_membership_role(
                team_id=config.manager.team_id,
                canonical_user_id="user_alpha",
                role="admin",
            )
            admin_view = await (await client.get("/api/web-chat/execution-ssh", headers=read_headers)).json()
            assert admin_view["can_manage"] is True
            assert len(admin_view["targets"]) == 1
            assert len(admin_view["sessions"]) == 1

            # Missing CSRF header is refused even for an admin.
            no_csrf = await client.post(
                "/api/web-chat/execution-ssh/sessions/ssh_sess_01/revoke",
                headers=read_headers,
                json={},
            )
            assert no_csrf.status == 403

            # Admin revokes session and target with valid CSRF + origin headers.
            revoked_sess = await client.post(
                "/api/web-chat/execution-ssh/sessions/ssh_sess_01/revoke",
                headers=write_headers,
                json={},
            )
            assert revoked_sess.status == 200
            revoked_sess_body = await revoked_sess.json()
            assert revoked_sess_body["revoked"] is True
            assert revoked_sess_body["terminated_live"] is True
            assert revoked_sess_body["sessions"][0]["status"] == "revoked"

            revoked_tgt = await client.post(
                "/api/web-chat/execution-ssh/targets/ssh_tgt_01/revoke",
                headers=write_headers,
                json={},
            )
            assert revoked_tgt.status == 200
            revoked_tgt_body = await revoked_tgt.json()
            assert revoked_tgt_body["revoked"] is True
            assert revoked_tgt_body["targets"][0]["status"] == "revoked"

            # Unknown id returns 404.
            missing = await client.post(
                "/api/web-chat/execution-ssh/targets/does_not_exist/revoke",
                headers=write_headers,
                json={},
            )
            assert missing.status == 404
    finally:
        person_changes.close()


@pytest.mark.asyncio
async def test_http_gateway_secure_broker_calls_execution_ssh_revoke_routes() -> None:
    """Verify HttpGatewaySecureBroker wires cleanly to register_execution_ssh_app routes."""

    seen: list[tuple[str, str, str, str]] = []

    class _FakeBrokerRuntime:
        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def revoke_session(self, *, session_id: str, revoked_by: str) -> dict[str, Any]:
            seen.append(("session", session_id, revoked_by, ""))
            return {"revoked": True, "session_id": session_id, "terminated_live": True}

        async def revoke_target(self, *, target_id: str, revoked_by: str) -> dict[str, Any]:
            seen.append(("target", target_id, revoked_by, ""))
            return {"revoked": True, "target_id": target_id}

    @web.middleware
    async def _auth_middleware(request: web.Request, handler):
        if request.path.startswith(EXECUTION_SSH_ROUTE_PREFIX):
            assert request.headers.get("Authorization") == "Bearer gw-broker-token"
        return await handler(request)

    broker_app = web.Application(middlewares=[_auth_middleware])
    register_execution_ssh_app(broker_app, _FakeBrokerRuntime())  # type: ignore[arg-type]

    async with TestServer(broker_app) as server:
        client = HttpGatewaySecureBroker(
            internal_base_url=str(server.make_url("/")).rstrip("/"),
            public_base_url=None,
            token="gw-broker-token",
        )
        sess_res = await client.revoke_execution_ssh_session(
            session_id="ssh_sess/special", revoked_by="usr_admin"
        )
        tgt_res = await client.revoke_execution_ssh_target(
            target_id="ssh_tgt/special", revoked_by="usr_admin"
        )

    assert sess_res["terminated_live"] is True
    assert tgt_res["revoked"] is True
    assert seen == [
        ("session", "ssh_sess/special", "usr_admin", ""),
        ("target", "ssh_tgt/special", "usr_admin", ""),
    ]


def test_gateway_execution_ssh_checks_both_change_kinds_and_bounds_inactive_rows(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)
    config = _make_config(tmp_path, authorization_enabled=False, ssh_enabled=False)
    repo = create_execution_control_repository(config.manager_storage_settings)
    for idx in range(55):
        _seed_target_and_session(
            repo,
            team_id=config.manager.team_id,
            now=now - timedelta(minutes=idx),
            target_id=f"ssh_tgt_{idx:03d}",
            session_id=f"ssh_sess_{idx:03d}",
            target_expires_at=(now - timedelta(seconds=1)) if idx >= 2 else (now + timedelta(hours=2)),
            session_status=(
                InteractiveSessionStatus.OPEN if idx < 2 else InteractiveSessionStatus.CLOSED
            ),
            session_expires_at=(now - timedelta(seconds=1)) if idx >= 2 else (now + timedelta(hours=2)),
        )

    asked: list[tuple[str | None, str]] = []

    class _RecordingChanges:
        def may_change(self, person_id: str | None, kind: str) -> bool:
            asked.append((person_id, kind))
            return True

    service = GatewayExecutionSSH(
        team_id=config.manager.team_id,
        config_path=config.config_path,
        repository=repo,
        person_changes=_RecordingChanges(),
        default_ssh_enabled=False,
        clock=lambda: now,
    )
    page = service.page(person_id="usr_admin")
    assert asked == [
        ("usr_admin", SSH_TARGET_ENROLL_CHANGE_KIND),
        ("usr_admin", SSH_INTERACTION_CHANGE_KIND),
    ]
    # 2 active + 50 bounded inactive = 52
    assert len(page.targets) == 52
    assert [t.status for t in page.targets[:2]] == ["active", "active"]
    assert all(t.status == "expired" for t in page.targets[2:])
    assert len(page.sessions) == 52
    assert all(s.is_active for s in page.sessions[:2])
    assert all(not s.is_active for s in page.sessions[2:])
