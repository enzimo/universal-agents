from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import struct

import pytest

from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_messages.contracts.common_v1 import contract_sha256
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
    AutomationJobV2,
    AutomationScheduleKind,
    AutomationScheduleV1,
    BuildAttestationV1,
    CodeArtifactV1,
    EnrolledSSHTargetV1,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    ManagedServiceV1,
    ServiceDesiredState,
    SSHConnectionTargetV1,
    SSHDelegationBoundsV1,
    SSHInteractionRecordV1,
    SSHSecretRecipe,
    SSHTargetLifecycle,
    SSHTargetPlatform,
    SSHTargetStatus,
    ExecutionLanguage,
    ExecutionRevisionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptStatus,
    RunAttemptV1,
    ssh_interaction_resource_digest,
    ssh_public_key_sha256_fingerprint,
)
from scripts.upgrade_schema_fixture import is_vector_table
from storage.execution_control_repository import (
    ImmutableRecordConflict,
    OldExecutionRecordFormat,
    SqliteExecutionControlRepository,
    StaleExecutionFence,
)
from tests.execution_access_support import activation as build_activation
from tests.upgrade_schemas import read_schema_statements


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _owner(team_id: str = "team_alpha") -> OwnerScopeV1:
    return OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id=team_id, team_id=team_id)


def _revision(team_id: str = "team_alpha") -> tuple[CodeArtifactV1, BuildAttestationV1, ExecutionRevisionV1]:
    owner = _owner(team_id)
    source = ArtifactReferenceV1(
        artifact_id="exec_art_source",
        kind=ArtifactKind.SOURCE,
        sha256=_SHA_A,
        size_bytes=10,
        media_type="text/x-python",
        storage_backend=ArtifactStorageBackend.FILESYSTEM,
        storage_key=f"source/aa/{_SHA_A}",
    )
    code = CodeArtifactV1(
        code_artifact_id="exec_code_source",
        owner=owner,
        language=ExecutionLanguage.PYTHON,
        source=source,
        source_name="main.py",
    )
    build = BuildAttestationV1.build(
        build_id="exec_build_source",
        owner=owner,
        source_artifact_id=code.code_artifact_id,
        source_sha256=source.sha256,
        profile_id="python.local",
        profile_digest=_SHA_B,
        toolchain_identity="python-test",
        builder_identity="builder-test",
        output=ArtifactReferenceV1(
            artifact_id="exec_art_build",
            kind=ArtifactKind.BUILD,
            sha256=_SHA_B,
            size_bytes=10,
            media_type="application/vnd.ua.python-source",
            storage_backend=ArtifactStorageBackend.FILESYSTEM,
            storage_key=f"build/bb/{_SHA_B}",
        ),
        entrypoint="main.py",
    )
    revision = ExecutionRevisionV1.build_revision(
        owner=owner,
        language=ExecutionLanguage.PYTHON,
        build=build,
        profile_id=build.profile_id,
        profile_digest=build.profile_digest,
        entrypoint=build.entrypoint,
    )
    return code, build, revision


def _job(revision: ExecutionRevisionV1, *, job_id: str = "exec_job_alpha") -> AutomationJobV2:
    now = datetime.now(timezone.utc)
    staged = AutomationJobV2(
        job_id=job_id,
        name="Periodic check",
        owner=revision.owner,
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        schedule=AutomationScheduleV1(
            schedule_kind=AutomationScheduleKind.INTERVAL,
            interval_seconds=60,
        ),
        next_run_at_utc=now - timedelta(seconds=5),
    )
    activation = build_activation(
        ExecutionActivationKind.RUN,
        resource_id=job_id,
        resource_digest=staged.activation_resource_digest(),
        owner_scope=revision.owner,
        mode=ChangeApprovalMode.HELD_ACCESS,
        expires_at=now + timedelta(hours=1),
    )
    return AutomationJobV2.model_validate(
        {**staged.model_dump(mode="python"), "activation": activation, "enabled": True}
    )


def _attempt(job: AutomationJobV2) -> RunAttemptV1:
    scheduled = job.next_run_at_utc or datetime.now(timezone.utc)
    return RunAttemptV1(
        occurrence_id=f"{job.job_id}:{scheduled.isoformat()}",
        job_id=job.job_id,
        owner=job.owner,
        revision_id=job.revision_id,
        revision_digest=job.revision_digest,
        job_generation=job.generation,
        scheduled_for_utc=scheduled,
    )


def _repository(tmp_path: Path) -> SqliteExecutionControlRepository:
    return SqliteExecutionControlRepository(str(tmp_path / "runtime" / "execution.db"))


def test_repository_persists_immutable_chain_and_scopes_reads(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    code, build, revision = _revision()

    repository.put_code_artifact(code)
    repository.put_build_attestation(build)
    repository.put_revision(revision)

    assert repository.get_revision(team_id="team_alpha", revision_id=revision.revision_id) == revision
    assert repository.get_revision(team_id="team_beta", revision_id=revision.revision_id) is None
    with pytest.raises(ImmutableRecordConflict, match="different content"):
        repository.put_code_artifact(code.model_copy(update={"source_name": "other.py"}))


def test_run_attempt_claim_renew_and_terminal_update_are_fenced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, _, revision = _revision()
    job = _job(revision)
    repository.create_job(job)
    attempt, _ = repository.create_run_attempt(_attempt(job))

    claimed = repository.claim_next_run_attempt(
        team_id="team_alpha",
        runner_id="runner_a",
        lease_token="token_a",
        lease_ttl_seconds=60,
    )
    assert claimed is not None
    assert claimed.run_id == attempt.run_id
    assert claimed.fence == 1

    renewed = repository.renew_run_attempt_lease(
        team_id="team_alpha",
        run_id=claimed.run_id,
        runner_id="runner_a",
        lease_token="token_a",
        fence=claimed.fence,
        lease_ttl_seconds=120,
    )
    assert renewed.lease_expires_at_utc > claimed.lease_expires_at_utc

    finished = renewed.model_copy(
        update={
            "status": RunAttemptStatus.SUCCEEDED,
            "finished_at_utc": datetime.now(timezone.utc),
            "exit_code": 0,
        }
    )
    repository.finish_run_attempt(
        finished,
        runner_id="runner_a",
        lease_token="token_a",
        fence=claimed.fence,
    )

    with pytest.raises(StaleExecutionFence, match="lease changed"):
        repository.finish_run_attempt(
            finished,
            runner_id="runner_a",
            lease_token="token_a",
            fence=claimed.fence,
        )


def test_expired_claim_increments_fence_and_invalidates_old_worker(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, _, revision = _revision()
    job = _job(revision)
    repository.create_job(job)
    repository.create_run_attempt(_attempt(job))
    old = repository.claim_next_run_attempt(
        team_id="team_alpha",
        runner_id="runner_old",
        lease_token="token_old",
        lease_ttl_seconds=60,
    )
    assert old is not None

    with repository._connect() as connection:
        persisted = RunAttemptV1.model_validate_json(
            connection.execute(
                "SELECT record_json FROM execution_run_attempts WHERE run_id = ?",
                (old.run_id,),
            ).fetchone()["record_json"]
        )
        expired = persisted.model_copy(
            update={"lease_expires_at_utc": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        connection.execute(
            "UPDATE execution_run_attempts SET lease_expires_at_utc = ?, record_json = ? WHERE run_id = ?",
            (
                expired.lease_expires_at_utc.isoformat(),
                json.dumps(expired.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                old.run_id,
            ),
        )
        connection.commit()

    replacement = repository.claim_next_run_attempt(
        team_id="team_alpha",
        runner_id="runner_new",
        lease_token="token_new",
        lease_ttl_seconds=60,
    )
    assert replacement is not None
    assert replacement.fence == old.fence + 1
    with pytest.raises(StaleExecutionFence, match="stale"):
        repository.renew_run_attempt_lease(
            team_id="team_alpha",
            run_id=old.run_id,
            runner_id="runner_old",
            lease_token="token_old",
            fence=old.fence,
            lease_ttl_seconds=60,
        )


def test_every_execution_table_has_first_class_owner_columns(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with repository._connect() as connection:
        table_names = (
            "execution_code_artifacts",
            "execution_build_attestations",
            "execution_revisions",
            "execution_automation_jobs",
            "execution_run_attempts",
            "execution_managed_services",
            "execution_interactive_sessions",
            "execution_ssh_interactions",
            "execution_ssh_targets",
        )
        for table_name in table_names:
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            assert {"team_id", "owner_scope_kind", "owner_id"} <= columns


def _old_activation(resource_id: str) -> dict[str, object]:
    """An activation as saved before access model 2 (removed permission store references)."""

    return {
        "schema_version": 1,
        "activation_id": "exec_activate_000000000000",
        "activation_digest": _SHA_A,
        "action": "execution.run",
        "resource_id": resource_id,
        "resource_digest": _SHA_B,
        "generation": 1,
        "owner": _owner().model_dump(mode="json"),
        "actor": {"principal_type": "Agent", "principal_id": "agent_old", "team_id": "team_alpha"},
        "authority_mode": "authenticated_authorization",
        "authorization_reference_id": "policy:7",
        "activated_at_utc": "2026-09-01T00:00:00+00:00",
    }


def _save_as_old_format(repository: SqliteExecutionControlRepository, table: str, id_column: str, record_id: str, **fields: object) -> None:
    with repository._connect() as connection:
        payload = json.loads(
            connection.execute(f"SELECT record_json FROM {table} WHERE {id_column} = ?", (record_id,)).fetchone()[
                "record_json"
            ]
        )
        payload.update({"schema_version": 1, **fields})
        connection.execute(
            f"UPDATE {table} SET record_json = ? WHERE {id_column} = ?", (json.dumps(payload), record_id)
        )
        connection.commit()


def _session(owner: OwnerScopeV1, session_id: str = "ssh_sess_old") -> InteractiveSessionV1:
    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="automation",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint="SHA256:ABCDEFGHIJKLMNOPQRSTUVWX",
        credential_binding_id="ssh_binding_primary",
    )
    digest = target.calculate_digest()
    return InteractiveSessionV1(
        session_id=session_id,
        owner=owner,
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id=session_id,
            resource_digest=digest,
            owner_scope=owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
        ),
    )


def test_old_format_records_are_refused_with_the_retire_command_and_retired_on_request(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, _, revision = _revision()
    job = _job(revision)
    repository.create_job(job)
    service = ManagedServiceV1(
        service_id="exec_svc_old",
        name="Old service",
        owner=revision.owner,
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        backend_workload_id="ua-exec-old",
    )
    repository.create_managed_service(service)
    session = _session(revision.owner)
    repository.create_interactive_session(session)
    _save_as_old_format(
        repository, "execution_automation_jobs", "job_id", job.job_id, activation=_old_activation(job.job_id)
    )
    _save_as_old_format(
        repository,
        "execution_managed_services",
        "service_id",
        service.service_id,
        desired_state="running",
        activation=_old_activation(service.service_id),
    )
    _save_as_old_format(
        repository,
        "execution_interactive_sessions",
        "session_id",
        session.session_id,
        status="open",
        connect_activation=_old_activation(session.session_id),
        interaction_activation=_old_activation(session.session_id),
    )

    with pytest.raises(OldExecutionRecordFormat, match="execution-retire-old-records"):
        repository.get_job(team_id="team_alpha", job_id=job.job_id)
    with pytest.raises(OldExecutionRecordFormat, match="schema version 1"):
        repository.list_managed_services(team_id="team_alpha")
    found = repository.old_format_records(team_id="team_alpha")
    assert sorted(found.records) == sorted(
        [
            ("execution_automation_jobs", job.job_id),
            ("execution_managed_services", service.service_id),
            ("execution_interactive_sessions", session.session_id),
        ]
    )
    assert "old_execution_records_present" in found.message()
    assert repository.old_format_records(team_id="team_beta").present is False

    retired = repository.retire_old_format_records(team_id="team_alpha")

    assert sorted(retired.records) == sorted(found.records)
    assert repository.old_format_records(team_id="team_alpha").present is False
    stored_job = repository.get_job(team_id="team_alpha", job_id=job.job_id)
    assert stored_job is not None
    assert (stored_job.enabled, stored_job.activation, stored_job.generation) == (False, None, job.generation + 1)
    assert repository.list_due_jobs(team_id="team_alpha", now=datetime.now(timezone.utc), limit=10) == ()
    stored_service = repository.get_managed_service(team_id="team_alpha", service_id=service.service_id)
    assert stored_service is not None
    assert (stored_service.desired_state, stored_service.activation) == (ServiceDesiredState.STOPPED, None)
    assert stored_service.backend_workload_id == "ua-exec-old", "the controller still knows what to stop"
    stored_session = repository.get_interactive_session(team_id="team_alpha", session_id=session.session_id)
    assert stored_session is not None
    assert (stored_session.status, stored_session.connect_activation) == (InteractiveSessionStatus.LOST, None)
    assert repository.retire_old_format_records(team_id="team_alpha").present is False


def test_ssh_typing_approvals_are_recorded_once_per_session(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    session = _session(_owner(), session_id="ssh_sess_typing")
    repository.create_interactive_session(session)
    approval = build_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=session.session_id,
        resource_digest=session.target_digest,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
    )

    assert repository.get_ssh_interaction(team_id="team_alpha", session_id=session.session_id) is None
    expected_record = SSHInteractionRecordV1(activation=approval)
    assert repository.put_ssh_interaction(approval) == expected_record
    assert repository.put_ssh_interaction(approval) == expected_record, "the same record again is accepted"
    with pytest.raises(ImmutableRecordConflict, match="different content"):
        repository.put_ssh_interaction(
            build_activation(
                ExecutionActivationKind.SSH_INTERACT,
                resource_id=session.session_id,
                resource_digest=session.target_digest,
                mode=ChangeApprovalMode.PERSON_APPROVAL,
            )
        )
    assert repository.get_ssh_interaction(team_id="team_alpha", session_id=session.session_id) == expected_record
    assert repository.get_ssh_interaction(team_id="team_beta", session_id=session.session_id) is None


def _enrolled_target(
    owner: OwnerScopeV1 | None = None,
    *,
    target_id: str = "ssh_tgt_prod_mac",
    host: str = "build-mac.internal",
    lifecycle: SSHTargetLifecycle = SSHTargetLifecycle.TEMPORARY,
    expires_at_utc: datetime | None = None,
) -> EnrolledSSHTargetV1:
    owner_scope = owner or _owner()
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
    seed_fields = {
        "target_id": target_id,
        "owner": owner_scope,
        "host": host,
        "port": 2222,
        "username": "admin",
        "platform": SSHTargetPlatform.MACOS,
        "host_key_algorithm": algo,
        "host_key_fingerprint": fp,
        "host_key_public_key": pub_line,
        "credential_key": "build_mac_ssh_key",
        "credential_owner_scope": owner_scope,
        "credential_public_key_fingerprint": fp,
        "bootstrap_credential_key": "build_mac_ssh_bootstrap",
        "bootstrap_credential_owner_scope": owner_scope,
        "lifecycle": lifecycle,
        "expires_at_utc": effective_expiry,
        "reason": "Bootstrap headless build host",
    }
    placeholder_activation = build_activation(
        ExecutionActivationKind.SSH_TARGET_ENROLL,
        resource_id=target_id,
        resource_digest=_SHA_A,
        owner_scope=owner_scope,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_target_enroll",
    )
    digest = EnrolledSSHTargetV1.model_construct(
        activation=placeholder_activation,
        **seed_fields,
    ).enrollment_digest()
    real_activation = build_activation(
        ExecutionActivationKind.SSH_TARGET_ENROLL,
        resource_id=target_id,
        resource_digest=digest,
        owner_scope=owner_scope,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_target_enroll",
    )
    return EnrolledSSHTargetV1(activation=real_activation, **seed_fields)


def _create_live_enrolled_session(
    repository: SqliteExecutionControlRepository,
    target: EnrolledSSHTargetV1,
    *,
    session_id: str,
    broker_id: str = "broker-a",
    lease_token: str = "lease-a",
) -> InteractiveSessionV1:
    conn_target = target.to_connection_target()
    target_digest = conn_target.calculate_digest()
    session = InteractiveSessionV1(
        session_id=session_id,
        owner=target.owner,
        target=conn_target,
        target_digest=target_digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id=session_id,
            resource_digest=target_digest,
            owner_scope=target.owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
        ),
    )
    repository.create_interactive_session(session)
    claimed = repository.claim_interactive_session(
        team_id=target.owner.team_id,
        session_id=session_id,
        broker_id=broker_id,
        lease_token=lease_token,
        lease_ttl_seconds=120,
    )
    return repository.update_claimed_interactive_session(
        claimed.model_copy(update={"status": InteractiveSessionStatus.OPEN}),
        broker_id=broker_id,
        lease_token=lease_token,
        fence=claimed.fence,
    )


def test_enrolled_ssh_targets_bootstrap_latch_and_cascade_revocation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    target = _enrolled_target()

    # Exact replay succeeds; conflicting content or wrong team fails closed.
    assert repository.put_ssh_target(target) == target
    assert repository.put_ssh_target(target) == target
    with pytest.raises(ImmutableRecordConflict, match="different content"):
        repository.put_ssh_target(_enrolled_target(host="other-host.internal"))
    assert repository.get_ssh_target(team_id="team_beta", target_id=target.target_id) is None

    # Create two live leased sessions referencing this enrolled target.
    session = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_enrolled_1", broker_id="broker-1", lease_token="tok-1"
    )
    session_other = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_other", broker_id="broker-2", lease_token="tok-2"
    )
    session_third = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_third", broker_id="broker-3", lease_token="tok-3"
    )
    bounds = SSHDelegationBoundsV1(
        delegated_agent_type="HostBootstrapper",
        delegated_task_id="work_req_123",
        allow_commands=True,
        allow_broker_sudo=True,
        allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
    )
    interaction_digest = ssh_interaction_resource_digest(
        target_digest=session.target_digest, bounds=bounds
    )
    interaction_act = build_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=session.session_id,
        resource_digest=interaction_digest,
        owner_scope=target.owner,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_interaction",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    stored_interaction = repository.put_ssh_interaction(interaction_act, bounds=bounds)
    assert stored_interaction.bounds == bounds
    assert stored_interaction.activation.binds_session(session, bounds=bounds)
    assert not stored_interaction.activation.binds_session(session, bounds=None)

    # Atomic password bootstrap claim: only one live session holds the claim until released or latched.
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=session.session_id
    ) is True
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=session_other.session_id
    ) is False
    assert repository.release_ssh_target_password_bootstrap_claim(
        team_id="team_alpha", target_id=target.target_id, session_id=session_other.session_id
    ) is False
    assert repository.release_ssh_target_password_bootstrap_claim(
        team_id="team_alpha", target_id=target.target_id, session_id=session.session_id
    ) is True
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=session_other.session_id
    ) is True

    latched = repository.mark_ssh_target_password_bootstrap_completed(
        team_id="team_alpha", target_id=target.target_id
    )
    assert latched is not None
    assert latched.password_bootstrap_completed_at_utc is not None
    assert latched.enrollment_digest() == target.enrollment_digest()
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=session_third.session_id
    ) is False

    # Revoking the target marks it revoked, preserves enrollment_digest, and cascades to sessions & interactions.
    revoked = repository.revoke_ssh_target(
        team_id="team_alpha", target_id=target.target_id, revoked_by="person_admin"
    )
    assert revoked is not None
    assert revoked.status == SSHTargetStatus.REVOKED
    assert revoked.revoked_by == "person_admin"
    assert revoked.is_usable() is False
    assert repository.list_ssh_targets(team_id="team_alpha", include_inactive=False) == ()
    assert len(repository.list_ssh_targets(team_id="team_alpha", include_inactive=True)) == 1

    sess_rev = repository.get_interactive_session_revocation(
        team_id="team_alpha", session_id=session.session_id
    )
    assert sess_rev is not None and sess_rev.revoked_by == "person_admin"
    revoked_interaction = repository.get_ssh_interaction(
        team_id="team_alpha", session_id=session.session_id
    )
    assert revoked_interaction is not None
    assert revoked_interaction.revoked_at_utc is not None
    assert revoked_interaction.is_active() is False


def test_password_bootstrap_claim_recovers_from_lost_revoked_and_expired_states(tmp_path: Path) -> None:
    """C35 & C39(b): Claims require live leased sessions, recover after restart/revoke, and refuse expired targets."""

    repository = _repository(tmp_path)
    target = _enrolled_target(target_id="ssh_tgt_claim_life")
    repository.put_ssh_target(target)

    # Unknown session is refused.
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id="ssh_sess_does_not_exist"
    ) is False

    # Unleased pending session is refused.
    unleased = InteractiveSessionV1(
        session_id="ssh_sess_unleased",
        owner=target.owner,
        target=target.to_connection_target(),
        target_digest=target.to_connection_target().calculate_digest(),
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_unleased",
            resource_digest=target.to_connection_target().calculate_digest(),
            owner_scope=target.owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
        ),
    )
    repository.create_interactive_session(unleased)
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=unleased.session_id
    ) is False

    # Session A claims, then broker restarts (marked lost); Session B can claim, while lost A cannot.
    sess_a = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_a", broker_id="broker-a", lease_token="tok-a"
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_a.session_id
    ) is True
    repository.mark_interactive_sessions_lost(
        team_id="team_alpha", updated_at_utc=datetime.now(timezone.utc)
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_a.session_id
    ) is False

    sess_b = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_b", broker_id="broker-b", lease_token="tok-b"
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_b.session_id
    ) is True

    # Revoking B clears the claim and refuses B from claiming again; live C can take over.
    repository.revoke_interactive_session(
        team_id="team_alpha", session_id=sess_b.session_id, revoked_by="person_admin"
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_b.session_id
    ) is False
    sess_c = _create_live_enrolled_session(
        repository, target, session_id="ssh_sess_c", broker_id="broker-c", lease_token="tok-c"
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_c.session_id
    ) is True

    # C35 & C39(b): Expired temporary target is excluded from active list and cannot be claimed.
    expired_target = _enrolled_target(
        target_id="ssh_tgt_expired",
        expires_at_utc=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    repository.put_ssh_target(expired_target)
    assert expired_target.effective_status() == SSHTargetStatus.EXPIRED
    active_ids = [item.target_id for item in repository.list_ssh_targets(team_id="team_alpha", include_inactive=False)]
    assert "ssh_tgt_expired" not in active_ids
    assert "ssh_tgt_claim_life" in active_ids
    sess_exp = _create_live_enrolled_session(
        repository, expired_target, session_id="ssh_sess_exp", broker_id="broker-e", lease_token="tok-e"
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=expired_target.target_id, session_id=sess_exp.session_id
    ) is False


def test_fresh_execution_store_logs_no_upgrade_column_additions(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
) -> None:
    """C37: Brand-new databases define all columns in CREATE TABLE without logging upgrade events."""

    with caplog.at_level(logging.INFO):
        _repository(tmp_path)
    if request.config.getoption("--upgrade-from", default=None):
        assert caplog.text.count("storage_column_added") == 5
    else:
        assert "storage_column_added" not in caplog.text


def test_session_revocation_columns_cannot_be_overwritten_by_lease_renewals(tmp_path: Path) -> None:
    """C05: Dedicated revocation columns survive concurrent lease writes and stop renewals."""

    repository = _repository(tmp_path)
    session = _session(_owner(), session_id="ssh_sess_revoke_col")
    repository.create_interactive_session(session)
    claimed = repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=session.session_id,
        broker_id="broker-1",
        lease_token="tok-1",
        lease_ttl_seconds=60,
    )
    opened = repository.update_claimed_interactive_session(
        claimed.model_copy(update={"status": InteractiveSessionStatus.OPEN}),
        broker_id="broker-1",
        lease_token="tok-1",
        fence=claimed.fence,
    )

    # Gateway revokes the session in the database while the broker holds the lease.
    rev = repository.revoke_interactive_session(
        team_id="team_alpha",
        session_id=session.session_id,
        revoked_by="person_admin",
    )
    assert rev is not None and rev.revoked_by == "person_admin"

    # A lease renewal (release=False) must fail closed and never clear revoked_at_utc.
    with pytest.raises(StaleExecutionFence, match="revoked"):
        repository.update_claimed_interactive_session(
            opened.model_copy(update={"transcript_bytes": 128}),
            broker_id="broker-1",
            lease_token="tok-1",
            fence=claimed.fence,
            release=False,
        )

    # Closing the revoked session (release=True) still persists final metadata and keeps revocation intact.
    closed = repository.update_claimed_interactive_session(
        opened.model_copy(update={"status": InteractiveSessionStatus.CLOSED, "transcript_bytes": 128}),
        broker_id="broker-1",
        lease_token="tok-1",
        fence=claimed.fence,
        release=True,
    )
    assert closed.lease_owner is None
    assert repository.get_interactive_session_revocation(
        team_id="team_alpha", session_id=session.session_id
    ) is not None

    # A revoked session can never be claimed again.
    with pytest.raises(StaleExecutionFence, match="revoked"):
        repository.claim_interactive_session(
            team_id="team_alpha",
            session_id=session.session_id,
            broker_id="broker-2",
            lease_token="tok-2",
            lease_ttl_seconds=60,
        )


def test_ssh_delegation_bounds_validation() -> None:
    bounds = SSHDelegationBoundsV1()
    assert (
        bounds.allow_commands,
        bounds.allow_typing,
        bounds.allow_password_bootstrap,
        bounds.allow_broker_sudo,
        bounds.allow_workspace_transfer,
        bounds.allowed_secret_recipes,
    ) == (False, False, False, False, False, ())
    with pytest.raises(ValueError, match="allow_broker_sudo"):
        SSHDelegationBoundsV1(allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,))


def test_upgrade_from_0_9_sqlite_fixture_preserves_job_service_session_and_interaction_digests(
    tmp_path: Path,
) -> None:
    """C13: Opening 0.10 repository on 0.9 schema upgrades columns and keeps every 0.9 digest valid."""

    fixture_statements = [
        stmt
        for stmt in read_schema_statements(Path("tests/fixtures/upgrade/0.9-sqlite.sql"))
        if not is_vector_table(stmt)
    ]
    db_path = tmp_path / "upgraded-0.9.db"
    conn = sqlite3.connect(str(db_path))
    try:
        for stmt in fixture_statements:
            conn.execute(stmt)
        owner = _owner()
        _, _, revision = _revision()
        job = _job(revision)
        service = ManagedServiceV1(
            service_id="exec_svc_09",
            name="Service from 0.9",
            owner=owner,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
        )
        # Build a 0.9 SSHConnectionTargetV1 JSON payload with ONLY the 0.9 keys.
        target_09_payload = {
            "host": "legacy.internal",
            "port": 22,
            "username": "deploy",
            "host_key_algorithm": "ssh-ed25519",
            "host_key_fingerprint": "SHA256:ABCDEFGHIJKLMNOPQRSTUVWX",
            "credential_binding_id": "legacy_disk_key",
            "remote_persistent_session_name": None,
            "allow_port_forwarding": False,
            "allow_agent_forwarding": False,
            "allow_x11_forwarding": False,
        }
        target_09_digest = contract_sha256(target_09_payload)
        connect_09 = build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_09",
            resource_digest=target_09_digest,
            owner_scope=owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
        )
        interact_09 = build_activation(
            ExecutionActivationKind.SSH_INTERACT,
            resource_id="ssh_sess_09",
            resource_digest=target_09_digest,
            owner_scope=owner,
            mode=ChangeApprovalMode.PERSON_APPROVAL,
            function="change:execution_ssh_interaction",
        )
        session_09_json = json.dumps(
            {
                "schema_version": 2,
                "session_id": "ssh_sess_09",
                "owner": owner.model_dump(mode="json"),
                "target": target_09_payload,
                "target_digest": target_09_digest,
                "connect_activation": connect_09.model_dump(mode="json"),
                "status": "pending",
                "fence": 0,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at_utc": None,
                "terminal_rows": 24,
                "terminal_columns": 80,
                "transcript_bytes": 0,
                "transcript_artifact": None,
                "created_by_task_id": "task_09",
                "created_at_utc": "2026-10-01T00:00:00+00:00",
                "updated_at_utc": "2026-10-01T00:00:00+00:00",
            },
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO execution_automation_jobs (
                job_id, team_id, owner_scope_kind, owner_id, generation, enabled,
                next_run_at_utc, paused_until_utc, expires_at_utc,
                activation_expires_at_utc, activation_revoked_at_utc, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                owner.team_id,
                owner.scope_kind.value,
                owner.owner_id,
                job.generation,
                1,
                job.next_run_at_utc.isoformat() if job.next_run_at_utc else None,
                None,
                None,
                None,
                None,
                job.model_dump_json(),
            ),
        )
        conn.execute(
            """
            INSERT INTO execution_managed_services (
                service_id, team_id, owner_scope_kind, owner_id, generation,
                observed_generation, desired_or_status, observed_state, active_fence,
                lease_owner, lease_token, lease_expires_at_utc, last_reconciled_at_utc, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service.service_id,
                owner.team_id,
                owner.scope_kind.value,
                owner.owner_id,
                service.generation,
                0,
                service.desired_state.value,
                service.observed_state.value,
                0,
                None,
                None,
                None,
                None,
                service.model_dump_json(),
            ),
        )
        conn.execute(
            """
            INSERT INTO execution_interactive_sessions (
                session_id, team_id, owner_scope_kind, owner_id, generation,
                observed_generation, desired_or_status, lease_owner, lease_token,
                lease_expires_at_utc, record_json
            ) VALUES (?, ?, ?, ?, 0, 0, 'pending', NULL, NULL, NULL, ?)
            """,
            ("ssh_sess_09", owner.team_id, owner.scope_kind.value, owner.owner_id, session_09_json),
        )
        conn.execute(
            """
            INSERT INTO execution_ssh_interactions (
                session_id, team_id, owner_scope_kind, owner_id, record_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("ssh_sess_09", owner.team_id, owner.scope_kind.value, owner.owner_id, interact_09.model_dump_json()),
        )
        conn.commit()
    finally:
        conn.close()

    # Open the upgraded 0.10 repository over the 0.9 database.
    repository = SqliteExecutionControlRepository(str(db_path))
    loaded_job = repository.get_job(team_id="team_alpha", job_id=job.job_id)
    assert loaded_job is not None and loaded_job.activation is not None
    loaded_service = repository.get_managed_service(team_id="team_alpha", service_id="exec_svc_09")
    assert loaded_service is not None
    loaded_session = repository.get_interactive_session(team_id="team_alpha", session_id="ssh_sess_09")
    assert loaded_session is not None
    assert loaded_session.target_digest == target_09_digest
    assert loaded_session.target.calculate_digest() == target_09_digest
    loaded_interaction = repository.get_ssh_interaction(team_id="team_alpha", session_id="ssh_sess_09")
    assert loaded_interaction is not None
    assert loaded_interaction.bounds is None
    assert loaded_interaction.revoked_at_utc is None
    assert loaded_interaction.activation.binds_session(loaded_session)
    assert repository.get_interactive_session_revocation(team_id="team_alpha", session_id="ssh_sess_09") is None
    assert repository.list_ssh_targets(team_id="team_alpha") == ()
