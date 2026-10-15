from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
    BuildAttestationV1,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptV1,
)
from runtime_hosting.kubernetes import (
    KubernetesBatchExecutionAdapter,
    KubernetesJobRenderer,
    KubernetesJobState,
    KubernetesOutcomeUnknown,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _profile() -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id="python.kubernetes.restricted",
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.KUBERNETES,
        runtime_executable="/usr/local/bin/python",
        toolchain_identity="cpython-test",
        runtime_image=f"registry.example.test/ua-worker@sha256:{_SHA_A}",
        artifact_fetcher_executable="/usr/local/bin/ua-artifact-fetch",
        kubernetes_artifact_pvc_name="ua-execution-artifacts",
        kubernetes_workload_pvc_name="ua-execution-data",
        kubernetes_namespace="ua-execution",
        kubernetes_service_account_name="ua-execution-worker",
        network_enabled=False,
    )


def _revision(profile: ExecutionProfileV1) -> ExecutionRevisionV1:
    owner = OwnerScopeV1(
        scope_kind=OwnerScopeKind.TEAM,
        owner_id="team_alpha",
        team_id="team_alpha",
    )
    output = ArtifactReferenceV1(
        artifact_id="exec_art_build",
        kind=ArtifactKind.BUILD,
        sha256=_SHA_B,
        size_bytes=100,
        media_type="application/vnd.ua.python-source",
            storage_backend=ArtifactStorageBackend.FILESYSTEM,
        storage_key=f"build/bb/{_SHA_B}",
    )
    build = BuildAttestationV1.build(
        owner=owner,
        source_artifact_id="exec_code_source",
        source_sha256=_SHA_A,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        toolchain_identity=profile.toolchain_identity,
        builder_identity="builder:test",
        output=output,
        entrypoint="main.py",
    )
    return ExecutionRevisionV1.build_revision(
        owner=owner,
        language=ExecutionLanguage.PYTHON,
        build=build,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        entrypoint=build.entrypoint,
        non_secret_env={"MODE": "test"},
    )


def _attempt(revision: ExecutionRevisionV1) -> RunAttemptV1:
    return RunAttemptV1(
        run_id="exec_run_kubernetes",
        occurrence_id="exec_job_kubernetes:1",
        job_id="exec_job_kubernetes",
        owner=revision.owner,
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        job_generation=1,
        scheduled_for_utc=datetime.now(timezone.utc),
        fence=3,
    )


def test_kubernetes_manifest_is_pinned_hardened_and_artifact_pvc_is_init_only() -> None:
    profile = _profile()
    revision = _revision(profile)
    attempt = _attempt(revision)
    renderer = KubernetesJobRenderer(network_policy_enforced=True)

    manifest = renderer.render(profile=profile, revision=revision, attempt=attempt)

    assert manifest["spec"]["backoffLimit"] == 0
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["containers"][0]["image"].endswith(f"@sha256:{_SHA_A}")
    assert {mount["name"] for mount in pod_spec["containers"][0]["volumeMounts"]} == {
        "input",
        "workload-data",
    }
    assert {item["name"]: item["value"] for item in pod_spec["containers"][0]["env"]}[
        "UA_DATA_DIR"
    ] == "/ua/data"
    assert {mount["name"] for mount in pod_spec["initContainers"][0]["volumeMounts"]} == {
        "input",
        "artifact-store",
    }
    artifact_volume = next(
        volume for volume in pod_spec["volumes"] if volume["name"] == "artifact-store"
    )
    assert artifact_volume["persistentVolumeClaim"]["claimName"] == (
        "ua-execution-artifacts"
    )
    data_volume = next(
        volume for volume in pod_spec["volumes"] if volume["name"] == "workload-data"
    )
    assert data_volume["persistentVolumeClaim"]["claimName"] == "ua-execution-data"
    digest = manifest["metadata"]["annotations"]["universal-agents.io/spec-digest"]
    assert digest == renderer.spec_digest(manifest)
    assert len(manifest["metadata"]["name"]) <= 63


def test_kubernetes_manifest_requires_real_network_policy_for_offline_profile() -> None:
    profile = _profile()

    with pytest.raises(Exception, match="NetworkPolicy"):
        KubernetesJobRenderer(network_policy_enforced=False).render(
            profile=profile,
            revision=_revision(profile),
            attempt=_attempt(_revision(profile)),
        )


class _FakeClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.states: list[KubernetesJobState] = []

    async def create_job(self, manifest: dict[str, Any]) -> KubernetesJobState:
        self.created.append(manifest)
        metadata = manifest["metadata"]
        state = KubernetesJobState(
            name=metadata["name"],
            uid="uid-1",
            spec_digest=metadata["annotations"]["universal-agents.io/spec-digest"],
            active=True,
            succeeded=False,
            failed=False,
        )
        self.states.append(state)
        return state

    async def get_job(self, *, namespace: str, name: str) -> KubernetesJobState | None:
        del namespace, name
        if len(self.states) == 1:
            terminal = KubernetesJobState(
                **{
                    **self.states[0].__dict__,
                    "active": False,
                    "succeeded": True,
                    "exit_code": 0,
                }
            )
            self.states.append(terminal)
        return self.states[-1] if self.states else None

    async def read_logs(
        self, *, namespace: str, name: str, maximum_bytes: int
    ) -> bytes:
        del namespace, name
        return b"kubernetes-ok\n"[:maximum_bytes]


@pytest.mark.asyncio
async def test_kubernetes_adapter_creates_monitors_and_records_exact_identity(tmp_path: Path) -> None:
    profile = _profile()
    revision = _revision(profile)
    attempt = _attempt(revision)
    client = _FakeClient()
    started: list[tuple[str, str | None, str | None]] = []

    async def record_started(name: str, uid: str | None, digest: str | None) -> None:
        started.append((name, uid, digest))

    result = await KubernetesBatchExecutionAdapter(
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        client=client,
        renderer=KubernetesJobRenderer(network_policy_enforced=True),
        poll_interval_seconds=0,
    ).execute(
        profile=profile,
        revision=revision,
        attempt=attempt,
        started_callback=record_started,
    )

    assert result.exit_code == 0
    assert result.stdout_preview == "kubernetes-ok\n"
    assert started == [(result.backend_workload_id, "uid-1", started[0][2])]
    assert started[0][2] == client.states[0].spec_digest
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_kubernetes_adapter_does_not_relaunch_unidentifiable_prior_workload(
    tmp_path: Path,
) -> None:
    profile = _profile()
    revision = _revision(profile)
    attempt = _attempt(revision).model_copy(
        update={
            "backend_workload_id": "ua-old",
            "backend_workload_uid": "old-uid",
            "backend_spec_digest": _SHA_A,
        }
    )
    client = _FakeClient()

    with pytest.raises(KubernetesOutcomeUnknown, match="cannot be identified"):
        await KubernetesBatchExecutionAdapter(
            artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
            client=client,
            renderer=KubernetesJobRenderer(network_policy_enforced=True),
            poll_interval_seconds=0,
        ).execute(profile=profile, revision=revision, attempt=attempt)

    assert client.created == []


@pytest.mark.asyncio
async def test_kubernetes_adapter_marks_ambiguous_create_outcome_unknown(
    tmp_path: Path,
) -> None:
    profile = _profile()
    revision = _revision(profile)
    attempt = _attempt(revision)

    class _AmbiguousClient(_FakeClient):
        async def create_job(self, manifest: dict[str, Any]) -> KubernetesJobState:
            del manifest
            raise TimeoutError("API response lost")

        async def get_job(
            self, *, namespace: str, name: str
        ) -> KubernetesJobState | None:
            del namespace, name
            return None

    with pytest.raises(KubernetesOutcomeUnknown, match="not proof"):
        await KubernetesBatchExecutionAdapter(
            artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
            client=_AmbiguousClient(),
            renderer=KubernetesJobRenderer(network_policy_enforced=True),
            poll_interval_seconds=0,
        ).execute(profile=profile, revision=revision, attempt=attempt)
