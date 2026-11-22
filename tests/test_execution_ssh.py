"""The SSH broker under access model 2.

Opening, reading and resizing need the session's connect activation, whose
held access is decided again on each call; typing also needs a person's
approval for that one session (``execution_ssh_interaction``, applied with
``apply_change``), stored apart from the session record. Closing only ends
access and needs no approval. The end-to-end tests use real Cedar and SQLite
through the agent tools.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from tests.tool_result_support import tool_json
from strands.interrupt import InterruptException

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from custom_agents.access_runtime import decide_call
from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_tools.providers import execution_control as tools
from custom_tools.providers.changes import apply_change
from tests.task_work_support import set_agent_ref
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard, ExecutionAuthorizationDenied
from runtime_hosting.execution_contracts import (
    ActivationRecordV2,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    SSHConnectionTargetV1,
    SSHInteractionRecordV1,
)
from runtime_hosting.ssh import SSHCredentialMaterial, SSHSessionBroker
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.change_approval_support import (
    CEDAR_BINARY,
    SignedTaskManager,
    ToolCall,
    decide,
    needs_cedar,
    stored_request,
    use_signed_contexts,
)
from tests.execution_access_support import (
    SSH_TARGET_ID,
    activation as build_activation,
    execution_instance,
    execution_lead,
    fake_ssh,
    grant_execution,
    host_key_fingerprint,
    revoke_grant,
    team_owner,
    write_ssh_material,
)

_AGREEMENT_GUARD = ExecutionAccessGuard(authorization_enabled=False)


def _session(key_bytes: bytes, *, session_id: str = "ssh_sess_test") -> InteractiveSessionV1:
    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(key_bytes),
        credential_binding_id="credential_binding_ssh",
    )
    digest = target.calculate_digest()
    return InteractiveSessionV1(
        session_id=session_id,
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id=session_id,
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )


def _typing_approval(session: InteractiveSessionV1) -> ActivationRecordV2:
    return build_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=session.session_id,
        resource_digest=session.target_digest,
        function="change:execution_ssh_interaction",
    )


class _MaterialProvider:
    def __init__(self, material: SSHCredentialMaterial):
        self.material = material

    @contextmanager
    def prepare(self, _session: InteractiveSessionV1) -> Iterator[SSHCredentialMaterial]:
        yield self.material


class _UnavailableMaterialProvider:
    @contextmanager
    def prepare(self, _session: InteractiveSessionV1) -> Iterator[SSHCredentialMaterial]:
        raise PermissionError("credential material unavailable")
        yield  # pragma: no cover


def _material(tmp_path: Path, *, key_bytes: bytes) -> SSHCredentialMaterial:
    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=key_bytes)
    return SSHCredentialMaterial(identity_file=identity, known_hosts_file=known_hosts)


def _broker(
    tmp_path: Path,
    repository: SqliteExecutionControlRepository,
    *,
    key_bytes: bytes = b"test-host-public-key",
    material_provider: Any = None,
    broker_id: str = "ssh-broker-a",
    **extra: Any,
) -> SSHSessionBroker:
    return SSHSessionBroker(
        repository=repository,
        authorization_guard=_AGREEMENT_GUARD,
        material_provider=material_provider or _MaterialProvider(_material(tmp_path, key_bytes=key_bytes)),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id=broker_id,
        ssh_executable=str(fake_ssh(tmp_path / "bin")),
        **extra,
    )


async def _read_until(broker: SSHSessionBroker, session_id: str, expected: bytes) -> bytes:
    output = b""
    for _ in range(3):
        output += await broker.read(session_id=session_id, maximum_bytes=4096, timeout_seconds=2)
        if expected in output:
            break
    return output


@pytest.mark.asyncio
async def test_typing_needs_an_approved_interaction_and_the_transcript_is_kept(tmp_path: Path) -> None:
    key_bytes = b"test-host-public-key"
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(key_bytes)
    repository.create_interactive_session(session)
    broker = _broker(tmp_path, repository, key_bytes=key_bytes)

    opened = await broker.open(session_id=session.session_id, lease_token="lease-token")
    assert opened.status == InteractiveSessionStatus.OPEN
    assert opened.fence == 1
    with pytest.raises(ExecutionAuthorizationDenied, match="needs a person's approval"):
        await broker.write(session_id=session.session_id, payload=b"hello\n")
    repository.put_ssh_interaction(_typing_approval(session))

    await broker.write(session_id=session.session_id, payload=b"hello\n")
    output = await _read_until(broker, session.session_id, b"remote:hello")
    closed = await broker.close(session_id=session.session_id)

    assert b"remote:hello" in output
    assert closed.status == InteractiveSessionStatus.CLOSED
    assert closed.lease_owner is None
    assert closed.transcript_artifact is not None
    assert b"remote:hello" in broker.artifact_store.read_verified(closed.transcript_artifact)


@pytest.mark.asyncio
async def test_a_typing_approval_for_another_session_does_not_count(tmp_path: Path) -> None:
    key_bytes = b"test-host-public-key"
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(key_bytes)
    repository.create_interactive_session(session)
    broker = _broker(tmp_path, repository, key_bytes=key_bytes)
    await broker.open(session_id=session.session_id, lease_token="lease-token")
    other = _session(key_bytes, session_id="ssh_sess_other")

    with pytest.raises(ExecutionAuthorizationDenied, match="another session"):
        broker.authorization_guard.require_ssh_interact(
            broker._sessions[session.session_id].session,
            SSHInteractionRecordV1(activation=_typing_approval(other)),
        )
    await broker.close(session_id=session.session_id)


@pytest.mark.asyncio
async def test_ssh_broker_fails_closed_on_host_key_mismatch_and_releases_lease(
    tmp_path: Path,
) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(b"expected-key")
    repository.create_interactive_session(session)
    broker = _broker(tmp_path, repository, key_bytes=b"different-key")

    with pytest.raises(PermissionError, match="pinned host key"):
        await broker.open(session_id=session.session_id, lease_token="lease-token")

    failed = repository.get_interactive_session(team_id="team_alpha", session_id=session.session_id)
    assert failed is not None
    assert failed.status == InteractiveSessionStatus.FAILED
    assert failed.lease_owner is None


@pytest.mark.asyncio
async def test_ssh_broker_releases_connecting_lease_when_material_is_unavailable(
    tmp_path: Path,
) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(b"expected-key")
    repository.create_interactive_session(session)
    broker = _broker(tmp_path, repository, material_provider=_UnavailableMaterialProvider())

    with pytest.raises(PermissionError, match="material unavailable"):
        await broker.open(session_id=session.session_id, lease_token="lease-token")

    failed = repository.get_interactive_session(team_id="team_alpha", session_id=session.session_id)
    assert failed is not None
    assert failed.status == InteractiveSessionStatus.FAILED
    assert failed.lease_owner is None


@pytest.mark.asyncio
async def test_an_agreement_session_is_refused_once_authorization_is_on(tmp_path: Path) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(b"test-host-public-key")
    repository.create_interactive_session(session)
    catalogs = CatalogSource(None)
    access = AccessService(
        store=AccessRepository(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        ),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),  # not reached: agreement is refused first
        team_id="team_alpha",
    )
    broker = _broker(tmp_path, repository)
    broker.authorization_guard = ExecutionAccessGuard(authorization_enabled=True, access=access, catalogs=catalogs)

    with pytest.raises(ExecutionAuthorizationDenied, match="Authorization is on now"):
        await broker.open(session_id=session.session_id, lease_token="lease-token")


@pytest.mark.asyncio
async def test_ssh_broker_disconnects_an_idle_expired_lease(tmp_path: Path) -> None:
    key_bytes = b"test-host-public-key"
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    session = _session(key_bytes)
    repository.create_interactive_session(session)
    broker = _broker(tmp_path, repository, key_bytes=key_bytes, lease_ttl_seconds=1)
    await broker.open(session_id=session.session_id, lease_token="lease-token")

    disconnected = None
    for _ in range(30):
        await asyncio.sleep(0.1)
        disconnected = repository.get_interactive_session(team_id="team_alpha", session_id=session.session_id)
        if disconnected is not None and disconnected.status == InteractiveSessionStatus.DISCONNECTED:
            break
    assert disconnected is not None
    assert disconnected.status == InteractiveSessionStatus.DISCONNECTED
    assert session.session_id not in broker._sessions


def test_ssh_broker_restart_marks_non_replayable_live_pty_lost(tmp_path: Path) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    pending = _session(b"test-host-public-key")
    repository.create_interactive_session(pending)
    claimed = repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=pending.session_id,
        broker_id="old-broker",
        lease_token="old-token",
        lease_ttl_seconds=300,
    )
    repository.update_claimed_interactive_session(
        claimed.model_copy(update={"status": InteractiveSessionStatus.OPEN}),
        broker_id="old-broker",
        lease_token="old-token",
        fence=claimed.fence,
    )
    broker = _broker(tmp_path, repository, broker_id="new-broker")

    recovered = broker.recover_after_restart()

    assert [item.status for item in recovered] == [InteractiveSessionStatus.LOST]
    persisted = repository.get_interactive_session(team_id="team_alpha", session_id=pending.session_id)
    assert persisted is not None
    assert persisted.status == InteractiveSessionStatus.LOST
    assert persisted.lease_owner is None


# -- end to end through the agent tools -------------------------------------------


@pytest.fixture
def lead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    use_signed_contexts(monkeypatch)
    tools._SSH_BROKERS.clear()
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=getattr(request, "param", True)))
    set_agent_ref(agent)
    yield agent
    set_agent_ref(None)
    tools._SSH_BROKERS.clear()


async def _apply(change_id: str, call: ToolCall, *, confirmed: bool = False) -> dict[str, Any]:
    return tool_json(await apply_change._tool_func(change_id, "Run the deployment commands", call, confirmed=confirmed))


async def _type(session_id: str, text: str) -> dict[str, Any]:
    return json.loads(await tools.write_execution_ssh_session._tool_func(session_id=session_id, text=text))


async def _read_text(session_id: str, expected: str) -> str:
    text = ""
    for _ in range(3):
        payload = json.loads(
            await tools.read_execution_ssh_session._tool_func(session_id=session_id, timeout_seconds=2)
        )
        assert "error" not in payload, payload
        text += payload["text"]
        if expected in text:
            break
    return text


@needs_cedar
@pytest.mark.asyncio
async def test_typing_waits_for_a_persons_approval_and_revoking_access_stops_the_session(lead: Any) -> None:
    grant = grant_execution(lead)
    decision = decide_call(lead, "write_execution_ssh_session", {"session_id": "any", "text": "ls"})
    assert decision is not None and decision.allowed, "the call is write; the approval is checked by the broker"
    session = json.loads(tools.create_execution_ssh_session._tool_func(target_id=SSH_TARGET_ID))
    session_id = session["session_id"]
    opened = json.loads(await tools.open_execution_ssh_session._tool_func(session_id=session_id))
    assert opened["status"] == "open", opened

    refused = await _type(session_id, "hello\n")
    assert refused["error_code"] == "execution_access_refused"
    assert "plan_execution_ssh_interaction" in refused["error"]

    staged = json.loads(
        tools.plan_execution_ssh_interaction._tool_func(
            session_id=session_id,
            reason="Deploy",
            ttl_seconds=600,
            allow_typing=True,
        )
    )
    assert staged["status"] == "staged", staged
    assert staged["change_id"].startswith("ssh_interaction_")
    assert any(host_key_fingerprint() in line for line in staged["effect"])
    call = ToolCall("tooluse_ssh")
    with pytest.raises(InterruptException):
        await _apply(staged["change_id"], call)
    assert stored_request(lead, call).change.kind == "execution_ssh_interaction"
    decide(lead, call)
    applied = await _apply(staged["change_id"], call)
    assert applied["status"] == "applied", applied
    assert applied["result"]["interaction"]["approval"]["mode"] == "person_approval"

    typed = await _type(session_id, "hello\n")
    assert typed == {"session_id": session_id, "written_bytes": 6}
    assert "remote:hello" in await _read_text(session_id, "remote:hello")
    listed = json.loads(tools.list_execution_ssh_sessions._tool_func())
    assert listed["sessions"][0]["typing_approved"] is True
    replay = await _apply(staged["change_id"], call)
    assert replay["status"] == "refused" and "already applied" in replay["reason"]

    revoke_grant(lead, grant)
    after_revoke = json.loads(
        await tools.read_execution_ssh_session._tool_func(session_id=session_id, timeout_seconds=0)
    )
    assert after_revoke["error_code"] == "execution_access_refused"
    assert "no longer holds write access" in after_revoke["error"]
    closed = json.loads(await tools.close_execution_ssh_session._tool_func(session_id=session_id))
    assert closed["status"] == "closed", "closing only ends access"


@pytest.mark.parametrize("lead", [False], indirect=True)
@pytest.mark.asyncio
async def test_with_authorization_off_agreement_allows_connecting_and_typing(lead: Any) -> None:
    without_agreement = json.loads(tools.create_execution_ssh_session._tool_func(target_id=SSH_TARGET_ID))
    assert "confirmed=true" in without_agreement["error"]
    session = json.loads(tools.create_execution_ssh_session._tool_func(target_id=SSH_TARGET_ID, confirmed=True))
    session_id = session["session_id"]
    assert session["connect_activation"]["approval"]["mode"] == ChangeApprovalMode.AGREEMENT.value
    await tools.open_execution_ssh_session._tool_func(session_id=session_id)
    assert "error" in await _type(session_id, "hello\n")

    staged = json.loads(
        tools.plan_execution_ssh_interaction._tool_func(
            session_id=session_id,
            reason="Deploy",
            ttl_seconds=600,
            allow_typing=True,
        )
    )
    needs_agreement = await _apply(staged["change_id"], ToolCall("tooluse_off"))
    applied = await _apply(staged["change_id"], ToolCall("tooluse_off"), confirmed=True)

    assert needs_agreement["status"] == "agreement_required"
    assert applied["status"] == "applied", applied
    assert (await _type(session_id, "hello\n"))["written_bytes"] == 6
    assert "remote:hello" in await _read_text(session_id, "remote:hello")
    await tools.close_execution_ssh_session._tool_func(session_id=session_id)


@pytest.mark.parametrize("lead", [False], indirect=True)
@pytest.mark.asyncio
async def test_a_session_is_used_only_for_the_person_it_was_created_for(lead: Any) -> None:
    session = json.loads(tools.create_execution_ssh_session._tool_func(target_id=SSH_TARGET_ID, confirmed=True))
    lead.task_manager = SignedTaskManager(
        team_id=lead.team_id,
        person_id="user_bob",
        storage_settings=lead.task_manager.storage_settings,
        roles=("member",),
    )

    refused = json.loads(await tools.open_execution_ssh_session._tool_func(session_id=session["session_id"]))

    assert refused["error"] == "SSH session not found."


# -- Checkpoint 6: Enrollment, Bounded Delegation, and ExecutionAccessGuard -------


def _framed_ed25519_pub(seed: bytes = b"\x09" * 32) -> str:
    import base64
    import struct

    wire = b"ssh-ed25519"
    blob = struct.pack(">I", len(wire)) + wire + struct.pack(">I", len(seed)) + seed
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} test@host"


def _seed_ssh_catalog(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_scope: Any,
    key_name: str = "build_mac_ssh_key",
    boot_name: str | None = "build_mac_ssh_bootstrap",
    pub_line: str | None = None,
) -> str:
    from secure_tool_broker import credential_catalog
    from secure_tool_broker.credential_catalog import (
        CatalogCredential,
        CatalogCredentialField,
        CatalogSourceRef,
        CredentialCatalog,
    )
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope

    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(db_path))
    key_pub = pub_line or _framed_ed25519_pub(b"\x11" * 32)
    creds: dict[str, CatalogCredential] = {
        key_name: CatalogCredential(
            display_name="Build Mac SSH Key",
            purpose="SSH key for build host",
            fields={
                "ssh_public_key": CatalogCredentialField(
                    kind="custom",
                    visibility="metadata_visible",
                    value_source="user_provided",
                    value=key_pub,
                ),
                "ssh_private_key": CatalogCredentialField(
                    kind="credential_file",
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_name=key_name, field_ref="ssh_private_key")
                    ],
                ),
            },
        )
    }
    if boot_name is not None:
        creds[boot_name] = CatalogCredential(
            display_name="Build Mac Bootstrap",
            purpose="One-time bootstrap password",
            fields={
                "username": CatalogCredentialField(
                    kind="username",
                    visibility="metadata_visible",
                    value_source="user_provided",
                    value="admin",
                ),
                "admin_password": CatalogCredentialField(
                    kind="password",
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[
                        CatalogSourceRef(source="sealbox", item_name=boot_name, field_ref="admin_password")
                    ],
                ),
            },
        )
    scope = (
        CredentialOwnerScope.user(owner_scope.owner_id)
        if owner_scope.scope_kind.value == "user"
        else CredentialOwnerScope.team(owner_scope.team_id)
    )
    credential_catalog.save_catalog(CredentialCatalog(credentials=creds), scope=scope)
    return key_pub


@needs_cedar
@pytest.mark.asyncio
async def test_ssh_target_enrollment_and_bounded_delegation_end_to_end_with_real_cedar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3 & A4: Real Cedar enrollment, Admin Inbox cards, non-admin refusal, and ExecutionAccessGuard."""

    from access_control.entities import PersonPrincipal
    from custom_agents import access_runtime
    from custom_agents.execution_changes import (
        SSHInteractionChangeService,
        SSHTargetEnrollChangeService,
    )
    from custom_agents.execution_control_service import ExecutionControlService
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from runtime_hosting.authorization import SSHCallerContext, SSHCapability
    from runtime_hosting.execution_contracts import (
        SSHSecretRecipe,
        SSHTargetLifecycle,
        SSHTargetPlatform,
        ssh_public_key_sha256_fingerprint,
    )

    use_signed_contexts(monkeypatch)
    tools._SSH_BROKERS.clear()
    instance = execution_instance(tmp_path, authorization_enabled=True)
    instance.config.execution_control.ssh.max_delegation_ttl_seconds = 1800
    instance.config.execution_control.ssh.allow_password_bootstrap = False
    instance.config.save(instance.config_path)

    lead = execution_lead(instance)
    set_agent_ref(lead)
    grant_execution(lead)

    exec_service = ExecutionControlService(lead)
    host_pub = _framed_ed25519_pub(b"\x22" * 32)
    host_algo, host_fp = ssh_public_key_sha256_fingerprint(host_pub)
    key_pub = _seed_ssh_catalog(
        instance.config.credential_catalog_database_path,
        monkeypatch,
        owner_scope=exec_service.owner,
    )
    _, key_fp = ssh_public_key_sha256_fingerprint(key_pub)

    # 1. Stage dynamic SSH target enrollment (using RFC 5737 public test IP 198.51.100.42).
    enroll_service = SSHTargetEnrollChangeService(lead)
    with pytest.raises(PermissionError, match="denied_cidrs|loopback"):
        enroll_service.stage(
            host="127.0.0.1",
            port=22,
            username="admin",
            platform=SSHTargetPlatform.MACOS,
            host_key_public_key=host_pub,
            credential_key="build_mac_ssh_key",
            lifecycle=SSHTargetLifecycle.TEMPORARY,
            expires_at_utc=datetime.now(timezone.utc) + timedelta(hours=2),
            reason="Should be refused by network guardrails",
        )

    target_expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    staged_target = enroll_service.stage(
        target_id="build_mac_01",
        host="198.51.100.42",
        port=2222,
        username="admin",
        platform=SSHTargetPlatform.MACOS,
        host_key_public_key=host_pub,
        credential_key="build_mac_ssh_key",
        bootstrap_credential_key="build_mac_ssh_bootstrap",
        lifecycle=SSHTargetLifecycle.TEMPORARY,
        expires_at_utc=target_expiry,
        reason="Bootstrap headless build Mac",
    )
    assert staged_target["status"] == "staged"
    assert staged_target["change_id"].startswith("ssh_target_enr_")
    effect_text = "\n".join(staged_target["effect"])
    assert "198.51.100.42:2222" in effect_text
    assert "admin@198.51.100.42:2222" in effect_text
    assert host_fp in effect_text
    assert "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" in effect_text
    assert "build_mac_ssh_key" in effect_text
    assert key_fp in effect_text
    assert "build_mac_ssh_bootstrap" in effect_text
    assert "temporary" in effect_text
    assert "Bootstrap headless build Mac" in effect_text

    # 2. Submit via apply_change -> pauses in Admin Inbox; non-admin member CANNOT approve!
    enroll_call = ToolCall("tooluse_enroll")
    with pytest.raises(InterruptException):
        await _apply(staged_target["change_id"], enroll_call)
    req = stored_request(lead, enroll_call)
    assert req.change is not None and req.change.kind == "execution_ssh_target_enroll"

    non_admin = PersonPrincipal("user_bob", lead.team_id, ("member",))
    with pytest.raises(PermissionError):
        access_runtime.runtime_requests(lead).decide(
            enroll_call.request_id, non_admin, AccessReviewChoice.APPROVE
        )

    # Admin approves -> target persisted in execution_ssh_targets.
    decide(lead, enroll_call)
    applied_target = await _apply(staged_target["change_id"], enroll_call)
    assert applied_target["status"] == "applied", applied_target
    assert applied_target["result"]["target"]["target_id"] == "build_mac_01"
    assert applied_target["result"]["target"]["host_key_algorithm"] == host_algo
    assert applied_target["result"]["target"]["host_key_fingerprint"] == host_fp

    # Replay after approval raises refused ("already applied"), even after latching bootstrap.
    exec_service.repository.mark_ssh_target_password_bootstrap_completed(
        team_id=lead.team_id, target_id="build_mac_01"
    )
    replay_enroll = await _apply(staged_target["change_id"], enroll_call)
    assert replay_enroll["status"] == "refused" and "already applied" in replay_enroll["reason"]

    # Wait: enroll a second unlatched target so we can also test password bootstrap guardrails.
    staged_target2 = enroll_service.stage(
        target_id="build_mac_02",
        host="198.51.100.43",
        port=22,
        username="admin",
        platform=SSHTargetPlatform.LINUX,
        host_key_public_key=host_pub,
        credential_key="build_mac_ssh_key",
        bootstrap_credential_key="build_mac_ssh_bootstrap",
        lifecycle=SSHTargetLifecycle.PERSISTENT,
        reason="Linux bootstrap target",
    )
    enroll_call2 = ToolCall("tooluse_enroll2")
    with pytest.raises(InterruptException):
        await _apply(staged_target2["change_id"], enroll_call2)
    decide(lead, enroll_call2)
    applied_target2 = await _apply(staged_target2["change_id"], enroll_call2)
    assert applied_target2["status"] == "applied"

    # Enrolled targets appear in list_ssh_targets without exposing secrets, and sessions can be created.
    targets = exec_service.list_ssh_targets()
    enrolled_summary = next(item for item in targets if item["target_id"] == "build_mac_02")
    assert enrolled_summary["source"] == "enrolled"
    assert enrolled_summary["host_key_fingerprint"] == host_fp
    assert enrolled_summary["credential_public_key_fingerprint"] == key_fp
    session = exec_service.create_ssh_session(target_id="build_mac_02")
    assert session.target.target_id == "build_mac_02"
    assert session.target.credential_owner_scope == exec_service.owner
    assert session.target.credential_public_key_fingerprint == key_fp

    # 3. Delegation validation: mandatory expiry, TTL cap, default-false capabilities, operator bootstrap toggle.
    interact_service = SSHInteractionChangeService(lead)
    with pytest.raises(ValueError, match="hard expiry"):
        interact_service.stage(session_id=session.session_id, reason="No expiry", allow_commands=True)
    with pytest.raises(ValueError, match="max_delegation_ttl_seconds"):
        interact_service.stage(
            session_id=session.session_id,
            reason="Too long",
            ttl_seconds=3600,
            allow_commands=True,
        )
    with pytest.raises(ValueError, match="Enable at least one"):
        interact_service.stage(
            session_id=session.session_id,
            reason="No caps",
            ttl_seconds=600,
        )
    with pytest.raises(PermissionError, match="allow_password_bootstrap is false"):
        interact_service.stage(
            session_id=session.session_id,
            reason="Bootstrap while disabled",
            ttl_seconds=600,
            allow_password_bootstrap=True,
        )

    # 4. Stage bounded delegation to specialist worker for one task with sudo + tailscale_up recipe.
    staged_del = interact_service.stage(
        session_id=session.session_id,
        reason="Join host to Tailscale",
        ttl_seconds=900,
        delegated_agent_type="HostBootstrapper",
        delegated_task_id="work_task_42",
        allow_commands=True,
        allow_broker_sudo=True,
        allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
    )
    del_effect = "\n".join(staged_del["effect"])
    assert "HostBootstrapper" in del_effect
    assert "work_task_42" in del_effect
    assert "allow_commands=ALLOWED" in del_effect
    assert "allow_typing=disabled" in del_effect
    assert "allow_password_bootstrap=disabled" in del_effect
    assert "allow_broker_sudo=ALLOWED" in del_effect
    assert "allow_workspace_transfer=disabled" in del_effect
    assert "tailscale_up" in del_effect
    assert "Security note:" in del_effect

    # Non-admin member cannot approve bounded delegation in Inbox under real Cedar.
    del_call = ToolCall("tooluse_del")
    with pytest.raises(InterruptException):
        await _apply(staged_del["change_id"], del_call)
    with pytest.raises(PermissionError):
        access_runtime.runtime_requests(lead).decide(
            del_call.request_id, non_admin, AccessReviewChoice.APPROVE
        )
    decide(lead, del_call)
    applied_del = await _apply(staged_del["change_id"], del_call)
    assert applied_del["status"] == "applied", applied_del

    # 5. ExecutionAccessGuard enforces caller type, task id, capability flags, recipes, expiry, and revocation.
    guard = exec_service.access_guard()
    stored_target = exec_service.repository.get_ssh_target(team_id=lead.team_id, target_id="build_mac_02")
    stored_interaction = exec_service.ssh_interaction(session.session_id)
    assert stored_target is not None and stored_interaction is not None

    valid_worker = SSHCallerContext(agent_type="HostBootstrapper", task_id="work_task_42", person_id="user_alice")
    guard.require_ssh_capability(
        session, stored_interaction, capability=SSHCapability.CONNECT, caller=valid_worker, enrolled_target=stored_target
    )
    guard.require_ssh_capability(
        session, stored_interaction, capability=SSHCapability.COMMAND, caller=valid_worker, enrolled_target=stored_target
    )
    guard.require_ssh_capability(
        session,
        stored_interaction,
        capability=SSHCapability.BROKER_SUDO,
        caller=valid_worker,
        enrolled_target=stored_target,
        secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
    )

    # Unapproved capabilities refused.
    for denied_cap in (
        SSHCapability.TYPING,
        SSHCapability.WORKSPACE_TRANSFER,
        SSHCapability.PASSWORD_BOOTSTRAP,
    ):
        with pytest.raises(ExecutionAuthorizationDenied):
            guard.require_ssh_capability(
                session,
                stored_interaction,
                capability=denied_cap,
                caller=valid_worker,
                enrolled_target=stored_target,
                allow_password_bootstrap_setting=True,
            )

    # Unauthorized agent types (another worker OR even TeamArchitect) and wrong task refused.
    for bad_caller in (
        None,
        SSHCallerContext(agent_type="OtherWorker", task_id="work_task_42", person_id="user_alice"),
        SSHCallerContext(agent_type="TeamArchitect", task_id="work_task_42", person_id="user_alice"),
        SSHCallerContext(agent_type="HostBootstrapper", task_id="wrong_task", person_id="user_alice"),
        SSHCallerContext(agent_type="HostBootstrapper", task_id="work_task_42", person_id="user_bob"),
    ):
        with pytest.raises(ExecutionAuthorizationDenied):
            guard.require_ssh_capability(
                session,
                stored_interaction,
                capability=SSHCapability.COMMAND,
                caller=bad_caller,
                enrolled_target=stored_target,
            )

    # Expired delegation refused.
    with pytest.raises(ExecutionAuthorizationDenied, match="expired"):
        guard.require_ssh_capability(
            session,
            stored_interaction,
            capability=SSHCapability.COMMAND,
            caller=valid_worker,
            enrolled_target=stored_target,
            now=datetime.now(timezone.utc) + timedelta(hours=2),
        )

    # Revoking the session immediately stops the guard, updates list_execution_ssh_sessions, and guides staging.
    rev_state = exec_service.repository.revoke_interactive_session(
        team_id=lead.team_id, session_id=session.session_id, revoked_by="user_alice"
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="revoked"):
        guard.require_ssh_capability(
            session,
            exec_service.ssh_interaction(session.session_id),
            capability=SSHCapability.COMMAND,
            caller=valid_worker,
            enrolled_target=stored_target,
            session_revocation=rev_state,
        )
    listed_sessions = json.loads(tools.list_execution_ssh_sessions._tool_func())
    revoked_entry = next(s for s in listed_sessions["sessions"] if s["session_id"] == session.session_id)
    assert revoked_entry["revoked"] is True
    assert revoked_entry["delegation_active"] is False
    assert revoked_entry["typing_approved"] is False

    replan = json.loads(
        tools.plan_execution_ssh_interaction._tool_func(
            session_id=session.session_id,
            reason="Try again",
            ttl_seconds=300,
            allow_commands=True,
        )
    )
    assert "revoked" in replan["error"] and "new session" in replan["error"]

    # Revoking the enrolled target stops target creation and guard checks.
    revoked_target = exec_service.repository.revoke_ssh_target(
        team_id=lead.team_id, target_id="build_mac_02", revoked_by="user_alice"
    )
    assert revoked_target is not None
    with pytest.raises(ExecutionAuthorizationDenied, match="revoked"):
        guard.require_ssh_target(revoked_target, session=session)
    with pytest.raises(PermissionError, match="revoked"):
        exec_service.create_ssh_session(target_id="build_mac_02")

    set_agent_ref(None)
    tools._SSH_BROKERS.clear()


def test_execution_access_guard_password_bootstrap_latch_and_host_key_mismatch() -> None:
    """A3 & A4: Guard refuses password bootstrap when disabled/latched and fails closed on host-key mismatch."""

    from runtime_hosting.authorization import SSHCallerContext, SSHCapability
    from runtime_hosting.execution_contracts import (
        EnrolledSSHTargetV1,
        SSHDelegationBoundsV1,
        SSHTargetLifecycle,
        SSHTargetPlatform,
        ssh_interaction_resource_digest,
        ssh_public_key_sha256_fingerprint,
    )

    host_pub = _framed_ed25519_pub(b"\x33" * 32)
    other_pub = _framed_ed25519_pub(b"\x44" * 32)
    algo, fp = ssh_public_key_sha256_fingerprint(host_pub)
    _, other_fp = ssh_public_key_sha256_fingerprint(other_pub)
    owner_scope = team_owner()
    seed = {
        "target_id": "ssh_tgt_guard_boot",
        "owner": owner_scope,
        "host": "198.51.100.77",
        "port": 22,
        "username": "admin",
        "platform": SSHTargetPlatform.WINDOWS,
        "host_key_algorithm": algo,
        "host_key_fingerprint": fp,
        "host_key_public_key": host_pub,
        "credential_key": "win_ssh_key",
        "credential_owner_scope": owner_scope,
        "bootstrap_credential_key": "win_boot_cred",
        "bootstrap_credential_owner_scope": owner_scope,
        "lifecycle": SSHTargetLifecycle.PERSISTENT,
        "expires_at_utc": None,
        "reason": "Windows bootstrap guard check",
    }
    placeholder = build_activation(
        ExecutionActivationKind.SSH_TARGET_ENROLL,
        resource_id="ssh_tgt_guard_boot",
        resource_digest="0" * 64,
        owner_scope=owner_scope,
        mode=ChangeApprovalMode.AGREEMENT,
        function="change:execution_ssh_target_enroll",
    )
    digest = EnrolledSSHTargetV1.model_construct(activation=placeholder, **seed).enrollment_digest()
    target = EnrolledSSHTargetV1(
        activation=build_activation(
            ExecutionActivationKind.SSH_TARGET_ENROLL,
            resource_id="ssh_tgt_guard_boot",
            resource_digest=digest,
            owner_scope=owner_scope,
            mode=ChangeApprovalMode.AGREEMENT,
            function="change:execution_ssh_target_enroll",
        ),
        **seed,
    )
    conn = target.to_connection_target()
    conn_digest = conn.calculate_digest()
    session = InteractiveSessionV1(
        session_id="ssh_sess_boot_guard",
        owner=owner_scope,
        target=conn,
        target_digest=conn_digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_boot_guard",
            resource_digest=conn_digest,
            owner_scope=owner_scope,
            mode=ChangeApprovalMode.AGREEMENT,
            function="create_execution_ssh_session",
        ),
    )
    bounds = SSHDelegationBoundsV1(allow_password_bootstrap=True)
    interaction = SSHInteractionRecordV1(
        activation=build_activation(
            ExecutionActivationKind.SSH_INTERACT,
            resource_id=session.session_id,
            resource_digest=ssh_interaction_resource_digest(target_digest=conn_digest, bounds=bounds),
            owner_scope=owner_scope,
            mode=ChangeApprovalMode.AGREEMENT,
            function="change:execution_ssh_interaction",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        ),
        bounds=bounds,
    )
    caller = SSHCallerContext(agent_type="TeamArchitect", task_id="task_change", person_id="user_alice")

    # Allowed when operator setting is True and target is unlatched.
    _AGREEMENT_GUARD.require_ssh_capability(
        session,
        interaction,
        capability=SSHCapability.PASSWORD_BOOTSTRAP,
        caller=caller,
        enrolled_target=target,
        allow_password_bootstrap_setting=True,
    )
    # Refused when operator setting is False.
    with pytest.raises(ExecutionAuthorizationDenied, match="allow_password_bootstrap is disabled"):
        _AGREEMENT_GUARD.require_ssh_capability(
            session,
            interaction,
            capability=SSHCapability.PASSWORD_BOOTSTRAP,
            caller=caller,
            enrolled_target=target,
            allow_password_bootstrap_setting=False,
        )
    # Refused once permanently latched.
    latched = target.model_copy(update={"password_bootstrap_completed_at_utc": datetime.now(timezone.utc)})
    with pytest.raises(ExecutionAuthorizationDenied, match="permanently latched off"):
        _AGREEMENT_GUARD.require_ssh_capability(
            session,
            interaction,
            capability=SSHCapability.PASSWORD_BOOTSTRAP,
            caller=caller,
            enrolled_target=latched,
            allow_password_bootstrap_setting=True,
        )
    # Refused if the enrolled target's pinned host key disagrees with the session's target.
    tampered_conn = conn.model_copy(
        update={"host_key_fingerprint": other_fp, "host_key_public_key": other_pub}
    )
    tampered_digest = tampered_conn.calculate_digest()
    tampered_session = InteractiveSessionV1(
        session_id="ssh_sess_boot_guard",
        owner=owner_scope,
        target=tampered_conn,
        target_digest=tampered_digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_boot_guard",
            resource_digest=tampered_digest,
            owner_scope=owner_scope,
            mode=ChangeApprovalMode.AGREEMENT,
            function="create_execution_ssh_session",
        ),
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="pinned host key"):
        _AGREEMENT_GUARD.require_ssh_target(target, session=tampered_session)
