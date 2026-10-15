from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

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
    ExecutionLanguage,
    ExecutionRevisionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptStatus,
    RunAttemptV1,
)
from storage.execution_control_repository import (
    ImmutableRecordConflict,
    OldExecutionRecordFormat,
    SqliteExecutionControlRepository,
    StaleExecutionFence,
)
from tests.execution_access_support import activation as build_activation


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
    digest = contract_sha256(target.model_dump(mode="json"))
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
    assert repository.put_ssh_interaction(approval) == approval
    assert repository.put_ssh_interaction(approval) == approval, "the same record again is accepted"
    with pytest.raises(ImmutableRecordConflict, match="different content"):
        repository.put_ssh_interaction(
            build_activation(
                ExecutionActivationKind.SSH_INTERACT,
                resource_id=session.session_id,
                resource_digest=session.target_digest,
                mode=ChangeApprovalMode.PERSON_APPROVAL,
            )
        )
    assert repository.get_ssh_interaction(team_id="team_alpha", session_id=session.session_id) == approval
    assert repository.get_ssh_interaction(team_id="team_beta", session_id=session.session_id) is None
