from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any

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
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    ManagedServiceV1,
    ServiceDesiredState,
    SSHConnectionTargetV1,
    SSHDelegationBoundsV1,
    SSHInteractionRecordV1,
    SSHSecretRecipe,
    SSHTargetStatus,
    ExecutionLanguage,
    ExecutionRevisionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptStatus,
    RunAttemptV1,
    ssh_interaction_resource_digest,
)
from scripts.upgrade_schema_fixture import is_vector_table
from storage.execution_control_repository import (
    ImmutableRecordConflict,
    OldExecutionRecordFormat,
    PostgresExecutionControlRepository,
    SqliteExecutionControlRepository,
    StaleExecutionFence,
)
from tests.execution_access_support import activation as build_activation
from tests.execution_ssh_repository_support import (
    build_enrolled_ssh_target,
    build_pending_enrolled_session,
    build_ssh_interaction_activation,
    create_live_enrolled_session,
)
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


_enrolled_target = build_enrolled_ssh_target
_create_live_enrolled_session = create_live_enrolled_session


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


def test_ssh_delegation_bounds_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Enable at least one"):
        SSHDelegationBoundsV1()

    recipe_only = SSHDelegationBoundsV1(allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,))
    assert (
        recipe_only.allow_commands,
        recipe_only.allow_typing,
        recipe_only.allow_password_bootstrap,
        recipe_only.allow_broker_sudo,
        recipe_only.allow_workspace_transfer,
        recipe_only.allowed_secret_recipes,
    ) == (False, False, False, False, False, (SSHSecretRecipe.TAILSCALE_UP,))

    # Recipe-only delegation round-trips cleanly through SQLite storage and binds its session digest.
    repository = SqliteExecutionControlRepository(str(tmp_path / "recipe-only.db"))
    target = _enrolled_target(target_id="ssh_tgt_recipe_only")
    repository.put_ssh_target(target)
    session = _create_live_enrolled_session(
        repository,
        target,
        session_id="ssh_sess_recipe_only",
        broker_id="broker-recipe",
        lease_token="tok-recipe",
    )
    interaction_digest = ssh_interaction_resource_digest(
        target_digest=session.target_digest, bounds=recipe_only
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
    saved = repository.put_ssh_interaction(interaction_act, bounds=recipe_only)
    loaded = repository.get_ssh_interaction(team_id=target.owner.team_id, session_id=session.session_id)
    assert loaded == saved
    assert loaded is not None and loaded.bounds == recipe_only
    assert loaded.activation.binds_session(session, bounds=recipe_only)

    # F5 / A5: Raw pre-change 0.10 SQLite row (allow_broker_sudo=true + allowed_secret_recipes=['tailscale_up'])
    # loads unchanged from execution_ssh_interactions and verifies its session binding digest.
    legacy_session = _create_live_enrolled_session(
        repository,
        target,
        session_id="ssh_sess_prechange_010",
        broker_id="broker-010",
        lease_token="tok-010",
    )
    raw_010_bounds_json = (
        '{"delegated_agent_type":"HostBootstrapper","delegated_task_id":"work_010",'
        '"allow_commands":true,"allow_typing":false,"allow_password_bootstrap":false,'
        '"allow_broker_sudo":true,"allow_workspace_transfer":false,'
        '"allowed_secret_recipes":["tailscale_up"],"reason":"Pre-change 0.10 sudo + recipe"}'
    )
    expected_010_bounds = SSHDelegationBoundsV1.model_validate_json(raw_010_bounds_json)
    raw_010_act = build_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=legacy_session.session_id,
        resource_digest=ssh_interaction_resource_digest(
            target_digest=legacy_session.target_digest,
            bounds=expected_010_bounds,
        ),
        owner_scope=target.owner,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_interaction",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    with sqlite3.connect(str(tmp_path / "recipe-only.db")) as raw_conn:
        raw_conn.execute(
            """
            INSERT INTO execution_ssh_interactions (
                session_id, team_id, owner_scope_kind, owner_id, record_json, delegation_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_session.session_id,
                target.owner.team_id,
                target.owner.scope_kind.value,
                target.owner.owner_id,
                raw_010_act.model_dump_json(),
                raw_010_bounds_json,
            ),
        )
    reloaded_010 = repository.get_ssh_interaction(
        team_id=target.owner.team_id, session_id=legacy_session.session_id
    )
    assert reloaded_010 is not None
    assert reloaded_010.bounds == expected_010_bounds
    assert reloaded_010.activation.binds_session(legacy_session, bounds=reloaded_010.bounds)


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


def test_f5_mark_interactive_sessions_lost_clears_leased_pending_and_bootstrap_claim(
    tmp_path: Path,
) -> None:
    """F5: Restart recovery clears stale leases and uncompleted bootstrap claims on leased PENDING sessions."""

    repository = _repository(tmp_path)
    target = _enrolled_target(target_id="ssh_tgt_f5_pending_boot")
    repository.put_ssh_target(target)
    conn_target = target.to_connection_target()
    pending = InteractiveSessionV1(
        session_id="ssh_sess_f5_pending",
        owner=target.owner,
        target=conn_target,
        target_digest=conn_target.calculate_digest(),
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_f5_pending",
            resource_digest=conn_target.calculate_digest(),
            owner_scope=target.owner,
            mode=ChangeApprovalMode.HELD_ACCESS,
        ),
    )
    repository.create_interactive_session(pending)

    # Simulate enroll_execution_ssh_key claiming a temp lease on a PENDING session and claiming bootstrap.
    claimed = repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=pending.session_id,
        broker_id="crashed-broker",
        lease_token="dead-token",
        lease_ttl_seconds=600,
    )
    assert claimed.status == InteractiveSessionStatus.PENDING
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha",
        target_id=target.target_id,
        session_id=pending.session_id,
    ) is True

    # Simulate broker crash & restart recovery.
    recovered = repository.mark_interactive_sessions_lost(
        team_id="team_alpha",
        updated_at_utc=datetime.now(timezone.utc),
    )
    assert recovered == ()

    # The PENDING session stays PENDING with its stale lease cleared, and bootstrap claim is released.
    after = repository.get_interactive_session(team_id="team_alpha", session_id=pending.session_id)
    assert after is not None
    assert after.status == InteractiveSessionStatus.PENDING
    assert after.lease_owner is None
    assert after.lease_token is None
    assert after.lease_expires_at_utc is None

    # Immediate retry on the same session succeeds without waiting for dead lease TTL expiry.
    retried = repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=pending.session_id,
        broker_id="restarted-broker",
        lease_token="fresh-token",
        lease_ttl_seconds=60,
    )
    assert retried.lease_owner == "restarted-broker"
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha",
        target_id=target.target_id,
        session_id=pending.session_id,
    ) is True


def test_f6_claim_rejects_terminal_sessions_close_unleased_persists_closed_and_pg_uses_for_update_cas(
    tmp_path: Path,
) -> None:
    """F6: Terminal sessions cannot be re-claimed, unleased PENDING closes atomically, and PG uses FOR UPDATE + fence CAS."""

    from runtime_hosting.execution_contracts import (
        ArtifactKind,
        ArtifactReferenceV1,
        ArtifactStorageBackend,
    )

    repository = _repository(tmp_path)
    owner = _owner()

    # 1) Command-only PENDING session closed via close_unleased_interactive_session persists CLOSED.
    pending = _session(owner, session_id="ssh_sess_f6_pending")
    repository.create_interactive_session(pending)
    closed = repository.close_unleased_interactive_session(
        team_id="team_alpha",
        session_id=pending.session_id,
    )
    assert closed is not None
    assert closed.status == InteractiveSessionStatus.CLOSED
    stored = repository.get_interactive_session(team_id="team_alpha", session_id=pending.session_id)
    assert stored is not None and stored.status == InteractiveSessionStatus.CLOSED

    # Cannot re-claim CLOSED session.
    with pytest.raises(StaleExecutionFence):
        repository.claim_interactive_session(
            team_id="team_alpha",
            session_id=pending.session_id,
            broker_id="broker-1",
            lease_token="tok-1",
            lease_ttl_seconds=60,
        )

    # 2) CLOSED session with transcript_artifact stays CLOSED with transcript intact on re-close/re-claim.
    with_transcript = _session(owner, session_id="ssh_sess_f6_transcript")
    repository.create_interactive_session(with_transcript)
    claimed = repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=with_transcript.session_id,
        broker_id="broker-1",
        lease_token="tok-t",
        lease_ttl_seconds=60,
    )
    artifact = ArtifactReferenceV1(
        artifact_id="art_transcript_f6",
        kind=ArtifactKind.LOG,
        sha256="a" * 64,
        size_bytes=42,
        media_type="text/plain",
        storage_backend=ArtifactStorageBackend.FILESYSTEM,
        storage_key="logs/f6.log",
    )
    repository.update_claimed_interactive_session(
        claimed.model_copy(
            update={
                "status": InteractiveSessionStatus.CLOSED,
                "transcript_artifact": artifact,
            }
        ),
        broker_id="broker-1",
        lease_token="tok-t",
        fence=claimed.fence,
        release=True,
    )
    idempotent_close = repository.close_unleased_interactive_session(
        team_id="team_alpha",
        session_id=with_transcript.session_id,
    )
    assert idempotent_close is not None
    assert idempotent_close.status == InteractiveSessionStatus.CLOSED
    assert idempotent_close.transcript_artifact == artifact
    with pytest.raises(StaleExecutionFence):
        repository.claim_interactive_session(
            team_id="team_alpha",
            session_id=with_transcript.session_id,
            broker_id="broker-2",
            lease_token="tok-2",
            lease_ttl_seconds=60,
        )

    # 3) Actively leased session refuses close_unleased_interactive_session with StaleExecutionFence.
    active = _session(owner, session_id="ssh_sess_f6_active_lease")
    repository.create_interactive_session(active)
    repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=active.session_id,
        broker_id="broker-live",
        lease_token="tok-live",
        lease_ttl_seconds=600,
    )
    with pytest.raises(StaleExecutionFence, match="leased"):
        repository.close_unleased_interactive_session(
            team_id="team_alpha",
            session_id=active.session_id,
        )

    # 4) Verify PostgreSQL claim_interactive_session emits FOR UPDATE, pending status guard, and generation CAS.
    executed_sql: list[str] = []

    class _FakeCursor:
        rowcount = 1

        def __init__(self, row: dict[str, Any] | None = None) -> None:
            self._row = row

        def fetchone(self) -> dict[str, Any] | None:
            return self._row

    class _FakePgConn:
        def __enter__(self) -> "_FakePgConn":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
            del params
            executed_sql.append(sql)
            if "CURRENT_TIMESTAMP" in sql:
                return _FakeCursor({"database_now": datetime.now(timezone.utc).isoformat()})
            if sql.lstrip().startswith("SELECT"):
                return _FakeCursor({"record_json": pending.model_dump_json()})
            return _FakeCursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    pg_repo = object.__new__(PostgresExecutionControlRepository)
    pg_repo._connect = lambda: _FakePgConn()  # type: ignore[method-assign]
    pg_repo.claim_interactive_session(
        team_id="team_alpha",
        session_id=pending.session_id,
        broker_id="pg-broker",
        lease_token="pg-tok",
        lease_ttl_seconds=60,
    )
    select_sql = next(sql for sql in executed_sql if "SELECT record_json" in sql)
    update_sql = next(sql for sql in executed_sql if "UPDATE execution_interactive_sessions" in sql)
    assert "FOR UPDATE" in select_sql
    assert "desired_or_status = %s" in select_sql
    assert "desired_or_status = %s" in update_sql
    assert "AND generation = %s" in update_sql


_SSH_TABLE_LOCK_RANK = {
    "execution_ssh_targets": 1,
    "execution_interactive_sessions": 2,
    "execution_ssh_interactions": 3,
}
_LOCK_TABLE_PATTERN = re.compile(
    r"\b(?:UPDATE|FROM|INTO)\s+(execution_ssh_targets|execution_interactive_sessions|execution_ssh_interactions)\b",
    re.IGNORECASE,
)


class _RecordingCursor:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _RecordingPostgresConnection:
    """Fake PostgreSQL connection that records every SQL statement, params, commit, and rollback."""

    def __init__(
        self,
        handler: Callable[[str, tuple[Any, ...]], _RecordingCursor],
    ) -> None:
        self._handler = handler
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "_RecordingPostgresConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _RecordingCursor:
        self.statements.append((sql, params))
        return self._handler(sql, params)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_recording_pg_repo(
    handler: Callable[[str, tuple[Any, ...]], _RecordingCursor],
) -> tuple[PostgresExecutionControlRepository, _RecordingPostgresConnection]:
    conn = _RecordingPostgresConnection(handler)
    repo = object.__new__(PostgresExecutionControlRepository)
    setattr(repo, "_connect", lambda: conn)
    return repo, conn


def _extract_ssh_lock_events(
    statements: list[tuple[str, tuple[Any, ...]]],
) -> list[tuple[str, str, str, tuple[Any, ...]]]:
    events: list[tuple[str, str, str, tuple[Any, ...]]] = []
    for sql, params in statements:
        normalized = " ".join(sql.split())
        upper = normalized.upper()
        match = _LOCK_TABLE_PATTERN.search(normalized)
        if match is None:
            continue
        table = match.group(1)
        if upper.startswith("SELECT"):
            if "FOR UPDATE" in upper:
                events.append(("FOR UPDATE", table, normalized, params))
            elif "FOR SHARE" in upper:
                events.append(("FOR SHARE", table, normalized, params))
            continue
        for verb in ("UPDATE", "DELETE", "INSERT"):
            if upper.startswith(verb):
                events.append((verb, table, normalized, params))
                break
    return events


def _assert_canonical_ssh_lock_order(
    statements: list[tuple[str, tuple[Any, ...]]],
    *,
    allow_already_locked_target_update: str | None = None,
) -> list[tuple[str, str, str, tuple[Any, ...]]]:
    events = _extract_ssh_lock_events(statements)
    assert events, "Expected at least one SSH row-locking statement."
    locked_targets: set[str] = set()
    max_rank = 0
    for lock_kind, table, normalized, params in events:
        if table == "execution_ssh_targets" and lock_kind == "FOR UPDATE" and params:
            locked_targets.add(str(params[0]))
        rank = _SSH_TABLE_LOCK_RANK[table]
        if rank < max_rank:
            is_allowed_reupdate = (
                allow_already_locked_target_update is not None
                and table == "execution_ssh_targets"
                and lock_kind == "UPDATE"
                and allow_already_locked_target_update in locked_targets
                and allow_already_locked_target_update in params
            )
            assert is_allowed_reupdate, (
                f"Non-canonical lock order: {lock_kind} on {table} after rank {max_rank}: {normalized}"
            )
        else:
            max_rank = rank
    return events


def test_postgres_revoke_ssh_target_locks_tables_canonically_in_sql_order() -> None:
    """#188 / A5 / C03: revoke_ssh_target locks targets, then all sessions in SQL order, then all interactions."""

    now = datetime.now(timezone.utc)
    target = build_enrolled_ssh_target(target_id="ssh_tgt_order_1")
    other_target = build_enrolled_ssh_target(target_id="ssh_tgt_order_2", host="other.internal")
    # Return matching rows in non-ASCII order to verify iteration preserves SQL ORDER BY without Python re-sorting.
    sess_b = build_pending_enrolled_session(target, session_id="ssh_sess_b")
    sess_a = build_pending_enrolled_session(target, session_id="ssh_sess_a")
    sess_other = build_pending_enrolled_session(other_target, session_id="ssh_sess_other")
    legacy_v1_json = json.dumps({"schema_version": 1, "session_id": "ssh_sess_v1"})

    def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
        del params
        if "CURRENT_TIMESTAMP" in sql:
            return _RecordingCursor(rows=[{"database_now": now.isoformat()}])
        if "FROM execution_ssh_targets" in sql:
            return _RecordingCursor(rows=[{"record_json": target.model_dump_json()}])
        if "FROM execution_interactive_sessions" in sql:
            return _RecordingCursor(
                rows=[
                    {"session_id": sess_b.session_id, "record_json": sess_b.model_dump_json()},
                    {"session_id": "ssh_sess_v1", "record_json": legacy_v1_json},
                    {"session_id": sess_other.session_id, "record_json": sess_other.model_dump_json()},
                    {"session_id": sess_a.session_id, "record_json": sess_a.model_dump_json()},
                ]
            )
        return _RecordingCursor(rowcount=1)

    pg_repo, conn = _make_recording_pg_repo(handler)
    revoked = pg_repo.revoke_ssh_target(
        team_id="team_alpha",
        target_id=target.target_id,
        revoked_by="person_admin",
    )

    assert revoked is not None and revoked.status == SSHTargetStatus.REVOKED
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(conn.statements)
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_ssh_interactions"),
        ("UPDATE", "execution_ssh_interactions"),
    ]
    scan_sql = next(
        " ".join(sql.split())
        for sql, _ in conn.statements
        if "FROM execution_interactive_sessions" in sql
    )
    assert "ORDER BY session_id" in scan_sql
    session_update_ids = [params[2] for kind, table, _, params in events if table == "execution_interactive_sessions"]
    interaction_update_ids = [params[2] for kind, table, _, params in events if table == "execution_ssh_interactions"]
    assert session_update_ids == ["ssh_sess_b", "ssh_sess_a"]
    assert interaction_update_ids == ["ssh_sess_b", "ssh_sess_a"]


def test_postgres_revoke_interactive_session_locks_targets_before_sessions_and_interactions() -> None:
    """#188 / A5 / C01: revoke_interactive_session locks unlatched targets first, then sessions, then interactions."""

    now = datetime.now(timezone.utc)
    target = build_enrolled_ssh_target(target_id="ssh_tgt_rev_sess")
    session = build_pending_enrolled_session(target, session_id="ssh_sess_rev_1")

    def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
        del params
        if "CURRENT_TIMESTAMP" in sql:
            return _RecordingCursor(rows=[{"database_now": now.isoformat()}])
        if "SELECT record_json FROM execution_interactive_sessions" in sql:
            return _RecordingCursor(rows=[{"record_json": session.model_dump_json()}])
        if "FROM execution_ssh_targets" in sql:
            return _RecordingCursor(
                rows=[
                    {
                        "target_id": target.target_id,
                        "password_bootstrap_claimed_by_session_id": session.session_id,
                    }
                ]
            )
        if "SELECT revoked_at_utc, revoked_by" in sql:
            return _RecordingCursor(
                rows=[{"revoked_at_utc": now.isoformat(), "revoked_by": "person_admin"}]
            )
        return _RecordingCursor(rowcount=1)

    pg_repo, conn = _make_recording_pg_repo(handler)
    revocation = pg_repo.revoke_interactive_session(
        team_id="team_alpha",
        session_id=session.session_id,
        revoked_by="person_admin",
    )

    assert revocation is not None and revocation.revoked_by == "person_admin"
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(conn.statements)
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_ssh_interactions"),
    ]
    target_lock_sql = events[0][2]
    assert "AND password_bootstrap_completed_at_utc IS NULL AND (target_id IN" in target_lock_sql
    assert "ORDER BY target_id FOR UPDATE" in target_lock_sql


def test_postgres_update_claimed_interactive_session_lock_order_and_rollback() -> None:
    """#188 / A5: release=True locks targets first and rolls back on stale fence; release=False skips targets."""

    target = build_enrolled_ssh_target(target_id="ssh_tgt_upd_rel")
    session = build_pending_enrolled_session(target, session_id="ssh_sess_upd_rel").model_copy(
        update={
            "status": InteractiveSessionStatus.CLOSED,
            "fence": 1,
            "lease_owner": "broker-1",
            "lease_token": "tok-1",
            "lease_expires_at_utc": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
    )

    def _release_handler(session_rowcount: int) -> Callable[[str, tuple[Any, ...]], _RecordingCursor]:
        def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
            del params
            if "FROM execution_ssh_targets" in sql:
                return _RecordingCursor(
                    rows=[
                        {
                            "target_id": target.target_id,
                            "password_bootstrap_claimed_by_session_id": session.session_id,
                        }
                    ]
                )
            if "UPDATE execution_interactive_sessions" in sql:
                return _RecordingCursor(rowcount=session_rowcount)
            return _RecordingCursor(rowcount=1)

        return handler

    # Valid release=True locks/clears targets before updating session and commits.
    pg_repo, conn = _make_recording_pg_repo(_release_handler(1))
    released = pg_repo.update_claimed_interactive_session(
        session,
        broker_id="broker-1",
        lease_token="tok-1",
        fence=1,
        release=True,
    )
    assert released.lease_owner is None
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(conn.statements)
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_interactive_sessions"),
    ]
    assert "AND password_bootstrap_completed_at_utc IS NULL AND (target_id IN" in events[0][2]
    assert "ORDER BY target_id FOR UPDATE" in events[0][2]

    # Stale release=True rolls back any tentative target update instead of committing.
    stale_repo, stale_conn = _make_recording_pg_repo(_release_handler(0))
    with pytest.raises(StaleExecutionFence):
        stale_repo.update_claimed_interactive_session(
            session,
            broker_id="broker-1",
            lease_token="wrong-tok",
            fence=1,
            release=True,
        )
    assert stale_conn.commits == 0 and stale_conn.rollbacks == 1
    _assert_canonical_ssh_lock_order(stale_conn.statements)

    # release=False stays on the single-table fast path and never touches execution_ssh_targets.
    renew_repo, renew_conn = _make_recording_pg_repo(_release_handler(1))
    renew_repo.update_claimed_interactive_session(
        session.model_copy(update={"status": InteractiveSessionStatus.OPEN}),
        broker_id="broker-1",
        lease_token="tok-1",
        fence=1,
        release=False,
    )
    assert renew_conn.commits == 1 and renew_conn.rollbacks == 0
    assert not any("execution_ssh_targets" in sql for sql, _ in renew_conn.statements)
    assert any("AND revoked_at_utc IS NULL" in sql for sql, _ in renew_conn.statements)


def test_postgres_close_unleased_interactive_session_lock_order_and_race_rollback() -> None:
    """#188 / A5: close_unleased locks targets before session FOR UPDATE and rolls back if newly leased."""

    now = datetime.now(timezone.utc)
    target = build_enrolled_ssh_target(target_id="ssh_tgt_close_unleased")
    unleased = build_pending_enrolled_session(target, session_id="ssh_sess_close_unleased")
    newly_leased = unleased.model_copy(
        update={
            "fence": 1,
            "lease_owner": "broker-racer",
            "lease_token": "tok-racer",
            "lease_expires_at_utc": now + timedelta(minutes=5),
        }
    )

    def _close_handler(*, leased_on_lock: bool) -> Callable[[str, tuple[Any, ...]], _RecordingCursor]:
        def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
            del params
            if "CURRENT_TIMESTAMP" in sql:
                return _RecordingCursor(rows=[{"database_now": now.isoformat()}])
            if "FROM execution_ssh_targets" in sql:
                return _RecordingCursor(
                    rows=[
                        {
                            "target_id": target.target_id,
                            "password_bootstrap_claimed_by_session_id": unleased.session_id,
                        }
                    ]
                )
            if "FROM execution_interactive_sessions" in sql:
                if "FOR UPDATE" in sql and leased_on_lock:
                    return _RecordingCursor(rows=[{"record_json": newly_leased.model_dump_json()}])
                return _RecordingCursor(rows=[{"record_json": unleased.model_dump_json()}])
            return _RecordingCursor(rowcount=1)

        return handler

    pg_repo, conn = _make_recording_pg_repo(_close_handler(leased_on_lock=False))
    closed = pg_repo.close_unleased_interactive_session(
        team_id="team_alpha",
        session_id=unleased.session_id,
    )
    assert closed is not None and closed.status == InteractiveSessionStatus.CLOSED
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(conn.statements)
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("FOR UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_interactive_sessions"),
    ]
    assert "ORDER BY target_id FOR UPDATE" in events[0][2]

    # If a concurrent broker leased the session between peek and FOR UPDATE, roll back target update.
    race_repo, race_conn = _make_recording_pg_repo(_close_handler(leased_on_lock=True))
    with pytest.raises(StaleExecutionFence, match="leased"):
        race_repo.close_unleased_interactive_session(
            team_id="team_alpha",
            session_id=unleased.session_id,
        )
    assert race_conn.commits == 0 and race_conn.rollbacks == 1
    _assert_canonical_ssh_lock_order(race_conn.statements)


def test_postgres_mark_interactive_sessions_lost_locks_multi_target_and_session_in_sql_order() -> None:
    """#188 / A5 / C03: mark_interactive_sessions_lost locks multiple targets then sessions in SQL ORDER BY order."""

    now = datetime.now(timezone.utc)
    tgt_b = build_enrolled_ssh_target(target_id="ssh_tgt_b", host="b.internal")
    tgt_a = build_enrolled_ssh_target(target_id="ssh_tgt_a", host="a.internal")
    open_sess = build_pending_enrolled_session(tgt_b, session_id="ssh_sess_2").model_copy(
        update={
            "status": InteractiveSessionStatus.OPEN,
            "fence": 1,
            "lease_owner": "broker-1",
            "lease_token": "tok-1",
            "lease_expires_at_utc": now + timedelta(minutes=5),
        }
    )
    pending_leased = build_pending_enrolled_session(tgt_a, session_id="ssh_sess_1").model_copy(
        update={
            "status": InteractiveSessionStatus.PENDING,
            "fence": 1,
            "lease_owner": "broker-2",
            "lease_token": "tok-2",
            "lease_expires_at_utc": now + timedelta(minutes=5),
        }
    )

    def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
        del params
        if "FROM execution_ssh_targets" in sql:
            # Non-ASCII order returned by SQL ORDER BY target_id to verify no Python re-sorting.
            return _RecordingCursor(
                rows=[
                    {
                        "target_id": tgt_b.target_id,
                        "password_bootstrap_claimed_by_session_id": open_sess.session_id,
                    },
                    {
                        "target_id": tgt_a.target_id,
                        "password_bootstrap_claimed_by_session_id": pending_leased.session_id,
                    },
                ]
            )
        if "FROM execution_interactive_sessions" in sql:
            # Non-ASCII order returned by SQL ORDER BY session_id to verify no Python re-sorting.
            return _RecordingCursor(
                rows=[
                    {"session_id": open_sess.session_id, "record_json": open_sess.model_dump_json()},
                    {"session_id": pending_leased.session_id, "record_json": pending_leased.model_dump_json()},
                ]
            )
        return _RecordingCursor(rowcount=1)

    pg_repo, conn = _make_recording_pg_repo(handler)
    recovered = pg_repo.mark_interactive_sessions_lost(
        team_id="team_alpha",
        updated_at_utc=now,
    )

    assert [item.session_id for item in recovered] == ["ssh_sess_2"]
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(conn.statements)
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("UPDATE", "execution_ssh_targets"),
        ("FOR UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_interactive_sessions"),
        ("UPDATE", "execution_interactive_sessions"),
    ]
    peek_sql = " ".join(conn.statements[0][0].split())
    assert "FROM execution_interactive_sessions" in peek_sql and "ORDER BY session_id" in peek_sql
    assert "FOR UPDATE" not in peek_sql
    assert "ORDER BY target_id FOR UPDATE" in events[0][2]
    assert "ORDER BY session_id FOR UPDATE" in events[3][2]
    target_update_ids = [params[0] for kind, table, _, params in events if kind == "UPDATE" and table == "execution_ssh_targets"]
    session_update_ids = [params[-2] for kind, table, _, params in events if kind == "UPDATE" and table == "execution_interactive_sessions"]
    assert target_update_ids == ["ssh_tgt_b", "ssh_tgt_a"]
    assert session_update_ids == ["ssh_sess_2", "ssh_sess_1"]


def test_postgres_claim_password_bootstrap_locks_target_before_sessions_on_takeover() -> None:
    """#188 / A5 / C03: claim_ssh_target_password_bootstrap locks target FOR UPDATE, checks claimer+holder FOR SHARE, then updates same target."""

    now = datetime.now(timezone.utc)
    target = build_enrolled_ssh_target(target_id="ssh_tgt_boot_takeover")
    claimer = build_pending_enrolled_session(target, session_id="ssh_sess_claimer").model_copy(
        update={
            "status": InteractiveSessionStatus.OPEN,
            "fence": 1,
            "lease_owner": "broker-new",
            "lease_token": "tok-new",
            "lease_expires_at_utc": now + timedelta(minutes=5),
        }
    )
    expired_holder = build_pending_enrolled_session(target, session_id="ssh_sess_holder").model_copy(
        update={
            "status": InteractiveSessionStatus.OPEN,
            "fence": 1,
            "lease_owner": "broker-old",
            "lease_token": "tok-old",
            "lease_expires_at_utc": now - timedelta(seconds=10),
        }
    )

    def handler(sql: str, params: tuple[Any, ...]) -> _RecordingCursor:
        del params
        if "CURRENT_TIMESTAMP" in sql:
            return _RecordingCursor(rows=[{"database_now": now.isoformat()}])
        if "FROM execution_ssh_targets" in sql:
            return _RecordingCursor(
                rows=[
                    {
                        "status": SSHTargetStatus.ACTIVE.value,
                        "expires_at_utc": (now + timedelta(hours=1)).isoformat(),
                        "revoked_at_utc": None,
                        "password_bootstrap_claimed_by_session_id": expired_holder.session_id,
                        "password_bootstrap_completed_at_utc": None,
                    }
                ]
            )
        if "FROM execution_interactive_sessions" in sql:
            return _RecordingCursor(
                rows=[
                    {
                        "session_id": claimer.session_id,
                        "desired_or_status": claimer.status.value,
                        "lease_owner": claimer.lease_owner,
                        "lease_expires_at_utc": claimer.lease_expires_at_utc.isoformat()
                        if claimer.lease_expires_at_utc
                        else None,
                        "revoked_at_utc": None,
                        "record_json": claimer.model_dump_json(),
                    },
                    {
                        "session_id": expired_holder.session_id,
                        "desired_or_status": expired_holder.status.value,
                        "lease_owner": expired_holder.lease_owner,
                        "lease_expires_at_utc": expired_holder.lease_expires_at_utc.isoformat()
                        if expired_holder.lease_expires_at_utc
                        else None,
                        "revoked_at_utc": None,
                        "record_json": expired_holder.model_dump_json(),
                    },
                ]
            )
        return _RecordingCursor(rowcount=1)

    pg_repo, conn = _make_recording_pg_repo(handler)
    claimed = pg_repo.claim_ssh_target_password_bootstrap(
        team_id="team_alpha",
        target_id=target.target_id,
        session_id=claimer.session_id,
    )

    assert claimed is True
    assert conn.commits == 1 and conn.rollbacks == 0
    events = _assert_canonical_ssh_lock_order(
        conn.statements,
        allow_already_locked_target_update=target.target_id,
    )
    assert [(kind, table) for kind, table, _, _ in events] == [
        ("FOR UPDATE", "execution_ssh_targets"),
        ("FOR SHARE", "execution_interactive_sessions"),
        ("UPDATE", "execution_ssh_targets"),
    ]
    session_lock_sql = events[1][2]
    assert "ORDER BY session_id FOR SHARE" in session_lock_sql
    assert set(events[1][3][1:]) == {claimer.session_id, expired_holder.session_id}
    assert events[2][3][1] == target.target_id


def test_sqlite_multi_session_target_revocation_isolation_and_coalesce(tmp_path: Path) -> None:
    """A2 / A5: SQLite target revocation cascades across multiple sessions, isolates other targets, and preserves COALESCE."""

    repository = _repository(tmp_path)
    target_a = build_enrolled_ssh_target(target_id="ssh_tgt_multi_a", host="a.internal")
    target_b = build_enrolled_ssh_target(target_id="ssh_tgt_multi_b", host="b.internal")
    repository.put_ssh_target(target_a)
    repository.put_ssh_target(target_b)

    sess_a1 = create_live_enrolled_session(repository, target_a, session_id="ssh_sess_a1", broker_id="b1", lease_token="t1")
    sess_a2 = create_live_enrolled_session(repository, target_a, session_id="ssh_sess_a2", broker_id="b2", lease_token="t2")
    sess_b1 = create_live_enrolled_session(repository, target_b, session_id="ssh_sess_b1", broker_id="b3", lease_token="t3")
    old_sess = _session(_owner(), session_id="ssh_sess_old_skip")
    repository.create_interactive_session(old_sess)
    _save_as_old_format(
        repository,
        "execution_interactive_sessions",
        "session_id",
        old_sess.session_id,
        connect_activation=_old_activation(old_sess.session_id),
    )

    for sess in (sess_a1, sess_a2, sess_b1):
        repository.put_ssh_interaction(build_ssh_interaction_activation(sess))

    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target_a.target_id, session_id=sess_a1.session_id
    ) is True

    first_time = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    second_time = first_time + timedelta(minutes=15)
    revoked_first = repository.revoke_ssh_target(
        team_id="team_alpha",
        target_id=target_a.target_id,
        revoked_by="person_admin_first",
        revoked_at_utc=first_time,
    )
    assert revoked_first is not None
    assert revoked_first.revoked_by == "person_admin_first"
    assert revoked_first.revoked_at_utc == first_time

    # Re-revoking with a different actor/time preserves the original revocation metadata via COALESCE.
    revoked_second = repository.revoke_ssh_target(
        team_id="team_alpha",
        target_id=target_a.target_id,
        revoked_by="person_admin_second",
        revoked_at_utc=second_time,
    )
    assert revoked_second is not None
    assert revoked_second.revoked_by == "person_admin_first"
    assert revoked_second.revoked_at_utc == first_time

    for sess_id in (sess_a1.session_id, sess_a2.session_id):
        rev = repository.get_interactive_session_revocation(team_id="team_alpha", session_id=sess_id)
        assert rev is not None
        assert rev.revoked_by == "person_admin_first"
        assert rev.revoked_at_utc == first_time
        inter = repository.get_ssh_interaction(team_id="team_alpha", session_id=sess_id)
        assert inter is not None
        assert inter.revoked_by == "person_admin_first"
        assert inter.revoked_at_utc == first_time

    # Sessions and interactions for target_b remain unrevoked.
    assert repository.get_interactive_session_revocation(team_id="team_alpha", session_id=sess_b1.session_id) is None
    inter_b = repository.get_ssh_interaction(team_id="team_alpha", session_id=sess_b1.session_id)
    assert inter_b is not None and inter_b.revoked_at_utc is None


def test_sqlite_rollback_preserves_bootstrap_claims_and_expiry_allows_takeover(tmp_path: Path) -> None:
    """A2 / A5: Failed fenced release/close keeps bootstrap claims intact; valid release/expired close/takeover clear or transfer them."""

    repository = _repository(tmp_path)
    target = build_enrolled_ssh_target(target_id="ssh_tgt_rollback_boot")
    repository.put_ssh_target(target)

    sess_1 = create_live_enrolled_session(repository, target, session_id="ssh_sess_rb_1", broker_id="b1", lease_token="tok-1")
    sess_2 = create_live_enrolled_session(repository, target, session_id="ssh_sess_rb_2", broker_id="b2", lease_token="tok-2")

    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_1.session_id
    ) is True
    # Idempotent re-claim by the same live session succeeds.
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_1.session_id
    ) is True

    # Stale-fence release=True must roll back and NOT clear sess_1's bootstrap claim.
    with pytest.raises(StaleExecutionFence):
        repository.update_claimed_interactive_session(
            sess_1.model_copy(update={"status": InteractiveSessionStatus.CLOSED}),
            broker_id="b1",
            lease_token="wrong-token",
            fence=sess_1.fence,
            release=True,
        )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_2.session_id
    ) is False

    # close_unleased on actively leased sess_1 must fail and NOT clear its bootstrap claim.
    with pytest.raises(StaleExecutionFence, match="leased"):
        repository.close_unleased_interactive_session(team_id="team_alpha", session_id=sess_1.session_id)
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_2.session_id
    ) is False

    # Valid release=True releases the lease AND clears the bootstrap claim so sess_2 can claim.
    repository.update_claimed_interactive_session(
        sess_1.model_copy(update={"status": InteractiveSessionStatus.CLOSED}),
        broker_id="b1",
        lease_token="tok-1",
        fence=sess_1.fence,
        release=True,
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_2.session_id
    ) is True

    # Expire sess_2's lease directly in SQLite -> sess_3 can take over, or close_unleased clears claim.
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    with repository._connect() as connection:
        expired_model = sess_2.model_copy(update={"lease_expires_at_utc": datetime.fromisoformat(expired_at)})
        connection.execute(
            "UPDATE execution_interactive_sessions SET lease_expires_at_utc = ?, record_json = ? WHERE session_id = ?",
            (expired_at, expired_model.model_dump_json(), sess_2.session_id),
        )
        connection.commit()

    closed_expired = repository.close_unleased_interactive_session(
        team_id="team_alpha",
        session_id=sess_2.session_id,
        disconnected=True,
    )
    assert closed_expired is not None and closed_expired.status == InteractiveSessionStatus.DISCONNECTED

    sess_3 = create_live_enrolled_session(repository, target, session_id="ssh_sess_rb_3", broker_id="b3", lease_token="tok-3")
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_3.session_id
    ) is True

    # Expire sess_3's lease without closing it -> live sess_4 takes over directly.
    with repository._connect() as connection:
        expired_3 = sess_3.model_copy(update={"lease_expires_at_utc": datetime.fromisoformat(expired_at)})
        connection.execute(
            "UPDATE execution_interactive_sessions SET lease_expires_at_utc = ?, record_json = ? WHERE session_id = ?",
            (expired_at, expired_3.model_dump_json(), sess_3.session_id),
        )
        connection.commit()

    sess_4 = create_live_enrolled_session(repository, target, session_id="ssh_sess_rb_4", broker_id="b4", lease_token="tok-4")
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id=target.target_id, session_id=sess_4.session_id
    ) is True
