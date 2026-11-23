"""Lightweight fixture builders for ExecutionControlRepository SSH tests and live validation.

Kept free of pytest, gateway, and agent imports so both pytest suites and the
standalone ``tests/live_postgres_storage_validation.py`` runner can reuse them.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import struct

from access_control.entities import operation_id
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
    enrolled_ssh_target_digest,
    ssh_interaction_resource_digest,
    ssh_public_key_sha256_fingerprint,
)
from storage.execution_control_repository import ExecutionControlRepository


_EXECUTION_TOOL = "fn:execution_control"


def team_owner_scope(team_id: str = "team_alpha") -> OwnerScopeV1:
    return OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id=team_id, team_id=team_id)


def build_ssh_activation(
    kind: ExecutionActivationKind,
    *,
    resource_id: str,
    resource_digest: str,
    owner_scope: OwnerScopeV1,
    mode: ChangeApprovalMode = ChangeApprovalMode.PERSON_APPROVAL,
    person_id: str = "user_alice",
    function: str = "change:execution_ssh_target_enroll",
    expires_at: datetime | None = None,
) -> ActivationRecordV2:
    """Build a schema-valid ActivationRecordV2 for SSH target, connect, or interaction records."""

    now = datetime.now(timezone.utc)
    approved_by = {
        ChangeApprovalMode.AGREEMENT: "agreement",
        ChangeApprovalMode.HELD_ACCESS: "TeamArchitect",
        ChangeApprovalMode.PERSON_APPROVAL: person_id,
    }[mode]
    return ActivationRecordV2.build(
        kind=kind,
        resource_id=resource_id,
        resource_digest=resource_digest,
        generation=1,
        owner=owner_scope,
        operation=operation_id(_EXECUTION_TOOL, function),
        holder=AccessHolderV1(
            agent_id="agent_team_lead",
            agent_type="TeamArchitect",
            person_id=person_id,
            person_roles=("owner",),
            task_id="task_ssh_repo_test",
        ),
        approval=ChangeApprovalRecordV1(
            mode=mode,
            approved_by=approved_by,
            approved_at_utc=now,
            access_request_id="access_req_ssh_test" if mode is ChangeApprovalMode.PERSON_APPROVAL else None,
        ),
        activated_at_utc=now - timedelta(seconds=1),
        expires_at_utc=expires_at,
    )


def build_enrolled_ssh_target(
    owner: OwnerScopeV1 | None = None,
    *,
    target_id: str = "ssh_tgt_prod_mac",
    host: str = "build-mac.internal",
    lifecycle: SSHTargetLifecycle = SSHTargetLifecycle.TEMPORARY,
    expires_at_utc: datetime | None = None,
) -> EnrolledSSHTargetV1:
    """Build a schema-valid EnrolledSSHTargetV1 with matching host key fingerprint and digest."""

    owner_scope = owner or team_owner_scope()
    wire_type = b"ssh-ed25519"
    key_blob = struct.pack(">I", len(wire_type)) + wire_type + struct.pack(">I", 32) + (b"\x05" * 32)
    key_b64 = base64.b64encode(key_blob).decode("ascii")
    pub_line = f"ssh-ed25519 {key_b64}"
    algo, fp = ssh_public_key_sha256_fingerprint(pub_line)
    effective_expiry = (
        expires_at_utc
        if expires_at_utc is not None
        else (
            datetime.now(timezone.utc) + timedelta(hours=2)
            if lifecycle == SSHTargetLifecycle.TEMPORARY
            else None
        )
    )
    credential_key = f"{target_id}_ssh_key"
    bootstrap_credential_key = f"{target_id}_ssh_bootstrap"
    reason = "Bootstrap headless build host"
    digest = enrolled_ssh_target_digest(
        target_id=target_id,
        owner=owner_scope,
        host=host,
        port=2222,
        username="admin",
        platform=SSHTargetPlatform.MACOS,
        host_key_algorithm=algo,
        host_key_fingerprint=fp,
        host_key_public_key=pub_line,
        credential_key=credential_key,
        credential_owner_scope=owner_scope,
        credential_public_key_fingerprint=fp,
        bootstrap_credential_key=bootstrap_credential_key,
        bootstrap_credential_owner_scope=owner_scope,
        lifecycle=lifecycle,
        expires_at_utc=effective_expiry,
        reason=reason,
    )
    real_activation = build_ssh_activation(
        ExecutionActivationKind.SSH_TARGET_ENROLL,
        resource_id=target_id,
        resource_digest=digest,
        owner_scope=owner_scope,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_target_enroll",
    )
    return EnrolledSSHTargetV1(
        activation=real_activation,
        target_id=target_id,
        owner=owner_scope,
        host=host,
        port=2222,
        username="admin",
        platform=SSHTargetPlatform.MACOS,
        host_key_algorithm=algo,
        host_key_fingerprint=fp,
        host_key_public_key=pub_line,
        credential_key=credential_key,
        credential_owner_scope=owner_scope,
        credential_public_key_fingerprint=fp,
        bootstrap_credential_key=bootstrap_credential_key,
        bootstrap_credential_owner_scope=owner_scope,
        lifecycle=lifecycle,
        expires_at_utc=effective_expiry,
        reason=reason,
    )


def build_pending_enrolled_session(
    target: EnrolledSSHTargetV1,
    *,
    session_id: str,
) -> InteractiveSessionV1:
    """Build an unleased PENDING InteractiveSessionV1 targeting ``target``."""

    conn_target = target.to_connection_target()
    target_digest = conn_target.calculate_digest()
    return InteractiveSessionV1(
        session_id=session_id,
        owner=target.owner,
        target=conn_target,
        target_digest=target_digest,
        connect_activation=build_ssh_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id=session_id,
            resource_digest=target_digest,
            owner_scope=target.owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
            function="open_execution_ssh_session",
        ),
    )


def create_live_enrolled_session(
    repository: ExecutionControlRepository,
    target: EnrolledSSHTargetV1,
    *,
    session_id: str,
    broker_id: str = "broker-a",
    lease_token: str = "lease-a",
    lease_ttl_seconds: int = 120,
    open_session: bool = True,
) -> InteractiveSessionV1:
    """Create, claim, and optionally transition to OPEN an interactive SSH session."""

    session = build_pending_enrolled_session(target, session_id=session_id)
    repository.create_interactive_session(session)
    claimed = repository.claim_interactive_session(
        team_id=target.owner.team_id,
        session_id=session_id,
        broker_id=broker_id,
        lease_token=lease_token,
        lease_ttl_seconds=lease_ttl_seconds,
    )
    if not open_session:
        return claimed
    return repository.update_claimed_interactive_session(
        claimed.model_copy(update={"status": InteractiveSessionStatus.OPEN}),
        broker_id=broker_id,
        lease_token=lease_token,
        fence=claimed.fence,
    )


def build_ssh_interaction_activation(
    session: InteractiveSessionV1,
    *,
    bounds: SSHDelegationBoundsV1 | None = None,
    expires_in_minutes: int = 30,
) -> ActivationRecordV2:
    """Build an SSH_INTERACT activation bound to ``session`` and optional ``bounds``."""

    digest = ssh_interaction_resource_digest(
        target_digest=session.target_digest,
        bounds=bounds,
    )
    return build_ssh_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=session.session_id,
        resource_digest=digest,
        owner_scope=session.owner,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_interaction",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes),
    )


__all__ = [
    "build_enrolled_ssh_target",
    "build_pending_enrolled_session",
    "build_ssh_activation",
    "build_ssh_interaction_activation",
    "create_live_enrolled_session",
    "team_owner_scope",
]
