from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_messages.contracts.common_v1 import contract_sha256
from runtime_hosting.execution_contracts import (
    ActivationRecordV2,
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
    AutomationJobV2,
    AutomationScheduleKind,
    AutomationScheduleV1,
    BuildAttestationV1,
    ExecutionActivationKind,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptStatus,
    RunAttemptV1,
    SSHConnectionTargetV1,
)
from tests.execution_access_support import activation as build_activation


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _owner() -> OwnerScopeV1:
    return OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id="team_alpha", team_id="team_alpha")


def _profile() -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id="python.local.restricted",
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.LOCAL,
        runtime_executable="/usr/bin/python3",
        toolchain_identity="cpython-3.13.7",
    )


def _build() -> BuildAttestationV1:
    profile = _profile()
    return BuildAttestationV1.build(
        owner=_owner(),
        source_artifact_id="exec_art_source",
        source_sha256=_SHA_A,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        toolchain_identity=profile.toolchain_identity,
        builder_identity="builder:test",
        output=ArtifactReferenceV1(
            kind=ArtifactKind.BUILD,
            sha256=_SHA_B,
            size_bytes=128,
            media_type="application/vnd.ua.python-source",
            storage_backend=ArtifactStorageBackend.FILESYSTEM,
            storage_key=f"build/{_SHA_B}",
        ),
        entrypoint="main.py",
    )


def _revision() -> ExecutionRevisionV1:
    build = _build()
    return ExecutionRevisionV1.build_revision(
        owner=_owner(),
        language=ExecutionLanguage.PYTHON,
        build=build,
        profile_id=build.profile_id,
        profile_digest=build.profile_digest,
        entrypoint=build.entrypoint,
    )


def _activation(
    *,
    resource_id: str,
    resource_digest: str,
    kind: ExecutionActivationKind = ExecutionActivationKind.RUN,
    generation: int = 1,
) -> ActivationRecordV2:
    now = datetime.now(timezone.utc)
    return build_activation(
        kind,
        resource_id=resource_id,
        resource_digest=resource_digest,
        generation=generation,
        mode=ChangeApprovalMode.HELD_ACCESS,
        activated_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )


def test_execution_profile_digest_detects_tampering() -> None:
    profile = _profile()

    with pytest.raises(ValidationError, match="profile digest mismatch"):
        ExecutionProfileV1.model_validate(
            {**profile.model_dump(mode="python"), "network_enabled": True}
        )


def test_execution_revision_is_content_addressed_and_exactly_bound() -> None:
    revision = _revision()

    assert revision.revision_id == f"exec_rev_{revision.revision_digest[:16]}"
    with pytest.raises(ValidationError, match="revision id does not match"):
        ExecutionRevisionV1.model_validate(
            {**revision.model_dump(mode="python"), "revision_id": "exec_rev_wrong"}
        )


def test_execution_revision_reserves_runtime_environment_names() -> None:
    build = _build()

    with pytest.raises(ValidationError, match="beginning with UA_"):
        ExecutionRevisionV1.build_revision(
            owner=_owner(),
            language=ExecutionLanguage.PYTHON,
            build=build,
            profile_id=build.profile_id,
            profile_digest=build.profile_digest,
            entrypoint=build.entrypoint,
            non_secret_env={"UA_DATA_DIR": "/tmp/override"},
        )


def test_one_shot_schedule_requires_an_exact_time() -> None:
    with pytest.raises(ValidationError, match="require one_shot_at_utc"):
        AutomationScheduleV1(schedule_kind=AutomationScheduleKind.ONE_SHOT)


def test_terminal_run_attempt_requires_finished_time() -> None:
    with pytest.raises(ValidationError, match="require finished_at_utc"):
        RunAttemptV1(
            occurrence_id="exec_job:2026-08-30T00:00:00Z",
            job_id="exec_job",
            owner=_owner(),
            revision_id="exec_rev",
            revision_digest=_SHA_A,
            job_generation=1,
            scheduled_for_utc=datetime.now(timezone.utc),
            status=RunAttemptStatus.SUCCEEDED,
        )


def test_ssh_session_binds_exact_target_and_loads_expired_authority() -> None:
    target = SSHConnectionTargetV1(
        host="server.example.test",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint="SHA256:abcdefghijklmnopqrstu",
        credential_binding_id="cred_binding_1",
    )
    target_digest = contract_sha256(target.model_dump(mode="json"))
    session_id = "ssh_sess_reconcile"
    session = InteractiveSessionV1(
        session_id=session_id,
        owner=_owner(),
        target=target,
        target_digest=target_digest,
        connect_activation=_activation(
            resource_id=session_id,
            resource_digest=target_digest,
            kind=ExecutionActivationKind.SSH_CONNECT,
        ),
        status=InteractiveSessionStatus.OPEN,
    )

    assert session.connect_activation is not None
    assert not session.connect_activation.is_active()
    with pytest.raises(ValidationError, match="SSH target digest mismatch"):
        InteractiveSessionV1.model_validate(
            {**session.model_dump(mode="python"), "target_digest": _SHA_B}
        )
    foreign_owner = OwnerScopeV1(
        scope_kind=OwnerScopeKind.TEAM,
        owner_id="team_beta",
        team_id="team_beta",
    )
    foreign_activation = build_activation(
        ExecutionActivationKind.SSH_CONNECT,
        resource_id=session_id,
        resource_digest=target_digest,
        owner_scope=foreign_owner,
        mode=ChangeApprovalMode.HELD_ACCESS,
    )
    with pytest.raises(ValidationError, match="does not bind this session"):
        InteractiveSessionV1.model_validate(
            {
                **session.model_dump(mode="python"),
                "status": InteractiveSessionStatus.PENDING,
                "connect_activation": foreign_activation,
            }
        )


def test_contract_schema_version_is_exact() -> None:
    artifact = _build().output

    with pytest.raises(ValidationError, match="Input should be 1"):
        ArtifactReferenceV1.model_validate(
            {**artifact.model_dump(mode="python"), "schema_version": 2}
        )


def test_activation_approval_must_fit_its_kind() -> None:
    digest = _SHA_A

    with pytest.raises(ValidationError, match="needs a person's approval"):
        build_activation(
            ExecutionActivationKind.SERVICE,
            resource_id="exec_svc_1",
            resource_digest=digest,
            mode=ChangeApprovalMode.HELD_ACCESS,
        )
    with pytest.raises(ValidationError, match="decided by held access"):
        build_activation(
            ExecutionActivationKind.RUN,
            resource_id="exec_job_1",
            resource_digest=digest,
            mode=ChangeApprovalMode.PERSON_APPROVAL,
        )
    with pytest.raises(ValidationError, match="must name one"):
        build_activation(
            ExecutionActivationKind.RUN,
            resource_id="exec_job_1",
            resource_digest=digest,
            mode=ChangeApprovalMode.HELD_ACCESS,
            person_id=None,
        )
    with pytest.raises(ValidationError, match="held for that same person"):
        build_activation(
            ExecutionActivationKind.RUN,
            resource_id="exec_job_1",
            resource_digest=digest,
            mode=ChangeApprovalMode.HELD_ACCESS,
            owner_scope=OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id="user_bob", team_id="team_alpha"),
        )
    agreed = build_activation(
        ExecutionActivationKind.SERVICE,
        resource_id="exec_svc_1",
        resource_digest=digest,
        mode=ChangeApprovalMode.AGREEMENT,
    )
    assert agreed.operation == "fn:execution_control#create_execution_job"


def test_records_carrying_authority_are_version_two() -> None:
    revision = _revision()
    job = AutomationJobV2(
        name="Version check",
        owner=_owner(),
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        schedule=AutomationScheduleV1(schedule_kind=AutomationScheduleKind.INTERVAL, interval_seconds=60),
    )

    assert job.schema_version == 2
    with pytest.raises(ValidationError, match="Input should be 2"):
        AutomationJobV2.model_validate({**job.model_dump(mode="python"), "schema_version": 1})
