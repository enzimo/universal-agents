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
from custom_messages.contracts.common_v1 import contract_sha256
from custom_tools.providers import execution_control as tools
from custom_tools.providers.changes import apply_change
from custom_tools.runtime_context import set_agent_ref
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard, ExecutionAuthorizationDenied
from runtime_hosting.execution_contracts import (
    ActivationRecordV2,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    SSHConnectionTargetV1,
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
    digest = contract_sha256(target.model_dump(mode="json"))
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
        broker.authorization_guard.require_ssh_interact(broker._sessions[session.session_id].session, _typing_approval(other))
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

    staged = json.loads(tools.plan_execution_ssh_interaction._tool_func(session_id=session_id, reason="Deploy"))
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

    staged = json.loads(tools.plan_execution_ssh_interaction._tool_func(session_id=session_id, reason="Deploy"))
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
