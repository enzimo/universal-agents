"""Shared setup for deterministic-execution tests under access model 2.

- ``execution_instance``: an instance with execution control on (a local
  Python profile, a Kubernetes profile, one pinned SSH target) and
  authorization on (real Cedar, the SQLite access store in the manager
  database) or off;
- ``execution_lead``: TeamArchitect working for the instance owner through a
  gateway-signed task context (``tests.change_approval_support``);
- ``grant_execution``: the owner gives TeamArchitect's agent type access to
  ``fn:execution_control``; ``grant_unconfined`` gives it full access to
  ``code:unconfined``, which jobs on the local (host) profile need;
- ``activation``: activation records for unit tests of the runtime checks.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from access_control.entities import operation_id
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    ActivationRecordV2,
    ExecutionActivationKind,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    OwnerScopeKind,
    OwnerScopeV1,
)
from runtime_settings.execution import ExecutionSSHTargetSettings
from tests.change_approval_support import CEDAR_BINARY, change_agent, owner

TEAM = "team_alpha"
PERSON = "user_alice"
LOCAL_PROFILE_ID = "python.local.test"
KUBERNETES_PROFILE_ID = "python.kubernetes.test"
SSH_TARGET_ID = "ops.primary"
SSH_HOST_KEY = b"test-host-public-key"
EXECUTION_TOOL = "fn:execution_control"
UNCONFINED_TOOL = "code:unconfined"
_SHA_A = "a" * 64


def local_profile() -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id=LOCAL_PROFILE_ID,
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.LOCAL,
        runtime_executable=sys.executable,
        toolchain_identity="cpython-test",
        allow_controller_host_execution=True,
        network_enabled=True,
    )


def kubernetes_profile() -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id=KUBERNETES_PROFILE_ID,
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.KUBERNETES,
        runtime_executable="/usr/local/bin/python",
        toolchain_identity="cpython-kubernetes-test",
        runtime_image=f"registry.example.test/worker@sha256:{_SHA_A}",
        artifact_fetcher_executable="/usr/local/bin/ua-artifact-fetch",
        kubernetes_artifact_pvc_name="ua-execution-artifacts",
        kubernetes_namespace="ua-execution",
        kubernetes_service_account_name="ua-worker",
        network_enabled=False,
    )


def host_key_fingerprint(key_bytes: bytes = SSH_HOST_KEY) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key_bytes).digest()).decode("ascii").rstrip("=")


def write_ssh_material(directory: Path, *, key_bytes: bytes = SSH_HOST_KEY) -> tuple[Path, Path]:
    """Broker-owned test key material: a private identity file and pinned known hosts."""

    directory.mkdir(parents=True, exist_ok=True)
    identity = directory / "identity"
    identity.write_text("broker-owned-test-key\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(
        f"ops.internal ssh-ed25519 {base64.b64encode(key_bytes).decode('ascii')}\n",
        encoding="utf-8",
    )
    return identity, known_hosts


def fake_ssh(directory: Path) -> Path:
    """An ``ssh`` stand-in that echoes one typed line back as ``remote:<line>``."""

    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "fake-ssh"
    executable.write_text(
        "#!/bin/sh\nIFS= read -r line\nprintf 'remote:%s\\n' \"$line\"\nsleep 5\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@dataclass
class ExecutionInstance:
    config: GatewayInstanceConfig
    config_path: Path

    @property
    def root(self) -> Path:
        return self.config.instance_root_path


def execution_instance(tmp_path: Path, *, authorization_enabled: bool) -> ExecutionInstance:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="execution-test",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
        manager_storage_sqlite_path="runtime/task_history.db",
    )
    config.user.canonical_user_id = PERSON
    config.authorization.enabled = authorization_enabled
    config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    config.execution_control.enabled = True
    config.execution_control.profiles = (local_profile(), kubernetes_profile())
    identity, known_hosts = write_ssh_material(root / "broker_credentials" / "ssh")
    config.execution_control.ssh.enabled = True
    config.execution_control.ssh.ssh_executable = str(fake_ssh(tmp_path / "bin"))
    config.execution_control.ssh.targets = (
        ExecutionSSHTargetSettings(
            target_id=SSH_TARGET_ID,
            host="ops.internal",
            username="automation",
            host_key_algorithm="ssh-ed25519",
            host_key_fingerprint=host_key_fingerprint(),
            credential_binding_id="ssh_binding_primary",
            identity_file=str(identity),
            known_hosts_file=str(known_hosts),
        ),
    )
    config_path = config.save(root / "instance_config.yaml")
    return ExecutionInstance(config=config, config_path=config_path)


def execution_lead(instance: ExecutionInstance) -> SimpleNamespace:
    """TeamArchitect working for the owner, in the instance's authorization mode."""

    return change_agent(
        instance.config,
        instance.config_path,
        authorization_enabled=instance.config.authorization.enabled,
    )


def grant_execution(
    agent: Any,
    *,
    level: AccessLevel = AccessLevel.WRITE,
    lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED,
    task_id: str | None = None,
) -> GrantV1:
    """The owner gives TeamArchitect's agent type access to fn:execution_control."""

    return _grant(agent, EXECUTION_TOOL, level=level, lifetime=lifetime, task_id=task_id)


def grant_unconfined(agent: Any, *, lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED) -> GrantV1:
    """The owner gives TeamArchitect's agent type full access to code:unconfined."""

    return _grant(agent, UNCONFINED_TOOL, level=AccessLevel.FULL, lifetime=lifetime, task_id=None)


def _grant(
    agent: Any, tool_id: str, *, level: AccessLevel, lifetime: GrantLifetime, task_id: str | None
) -> GrantV1:
    person = owner(agent)
    return access_runtime.runtime_access(agent).service.grant_by_person(
        person,
        GrantV1(
            grant_id="new",
            team_id=agent.team_id,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{agent.team_id}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
            level=level,
            lifetime=lifetime,
            task_id=task_id,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            created_at_utc=datetime.now(timezone.utc),
        ),
    )


def revoke_grant(agent: Any, grant: GrantV1) -> None:
    access_runtime.runtime_access(agent).service.revoke_by_person(owner(agent), grant.grant_id)


def team_owner() -> OwnerScopeV1:
    return OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id=TEAM, team_id=TEAM)


def activation(
    kind: ExecutionActivationKind,
    *,
    resource_id: str,
    resource_digest: str,
    generation: int = 1,
    owner_scope: OwnerScopeV1 | None = None,
    mode: ChangeApprovalMode = ChangeApprovalMode.AGREEMENT,
    person_id: str | None = PERSON,
    function: str = "create_execution_job",
    activated_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ActivationRecordV2:
    """An activation as the service or a change would record it."""

    now = datetime.now(timezone.utc)
    approved_by = {
        ChangeApprovalMode.AGREEMENT: "agreement",
        ChangeApprovalMode.HELD_ACCESS: "TeamArchitect",
        ChangeApprovalMode.PERSON_APPROVAL: person_id or "someone",
    }[mode]
    return ActivationRecordV2.build(
        kind=kind,
        resource_id=resource_id,
        resource_digest=resource_digest,
        generation=generation,
        owner=owner_scope or team_owner(),
        operation=operation_id(EXECUTION_TOOL, function),
        holder=AccessHolderV1(
            agent_id="agent_team_lead",
            agent_type="TeamArchitect",
            person_id=person_id,
            person_roles=("owner",),
            task_id="task_change",
        ),
        approval=ChangeApprovalRecordV1(
            mode=mode,
            approved_by=approved_by,
            approved_at_utc=now,
            access_request_id="access_req_test" if mode is ChangeApprovalMode.PERSON_APPROVAL else None,
        ),
        activated_at_utc=activated_at or now - timedelta(seconds=1),
        expires_at_utc=expires_at,
    )


__all__ = [
    "EXECUTION_TOOL",
    "ExecutionInstance",
    "KUBERNETES_PROFILE_ID",
    "LOCAL_PROFILE_ID",
    "PERSON",
    "SSH_HOST_KEY",
    "SSH_TARGET_ID",
    "TEAM",
    "activation",
    "execution_instance",
    "execution_lead",
    "fake_ssh",
    "grant_execution",
    "host_key_fingerprint",
    "kubernetes_profile",
    "local_profile",
    "revoke_grant",
    "team_owner",
    "write_ssh_material",
]
