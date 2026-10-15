"""Kubernetes Job adapter with exact identity adoption and no implicit retries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol

from custom_messages.contracts.common_v1 import contract_sha256
from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.execution_adapters import (
    BatchExecutionResult,
    ExecutionAdapterError,
    OutputCallback,
    StartedCallback,
)
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactStorageBackend,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    ManagedServiceV1,
    RunAttemptV1,
    ServiceDesiredState,
    ServiceKind,
    ServiceObservedState,
    ServiceRestartPolicy,
)
from runtime_hosting.services import ServiceBackendObservation


class KubernetesOutcomeUnknown(ExecutionAdapterError):
    """The controller cannot prove whether an earlier workload ran."""


@dataclass(frozen=True)
class KubernetesJobState:
    name: str
    uid: str
    spec_digest: str
    active: bool
    succeeded: bool
    failed: bool
    exit_code: int | None = None


class KubernetesJobClient(Protocol):
    async def create_job(self, manifest: dict[str, Any]) -> KubernetesJobState: ...
    async def get_job(self, *, namespace: str, name: str) -> KubernetesJobState | None: ...
    async def read_logs(
        self, *, namespace: str, name: str, maximum_bytes: int
    ) -> bytes: ...


@dataclass(frozen=True)
class KubernetesJobRenderer:
    network_policy_enforced: bool

    def render(
        self,
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        attempt: RunAttemptV1,
    ) -> dict[str, Any]:
        if profile.backend != ExecutionBackendKind.KUBERNETES:
            raise ExecutionAdapterError("Kubernetes renderer requires a Kubernetes profile.")
        if not profile.network_enabled and not self.network_policy_enforced:
            raise ExecutionAdapterError(
                "A network-disabled Kubernetes profile requires an enforced NetworkPolicy."
            )
        if revision.build.output.storage_backend != ArtifactStorageBackend.FILESYSTEM:
            raise ExecutionAdapterError(
                "Kubernetes PVC transport requires a filesystem artifact reference."
            )
        name = self.job_name(attempt.run_id, attempt.fence)
        labels = {
            "app.kubernetes.io/managed-by": "universal-agents",
            "universal-agents.io/run-id": self._label_value(attempt.run_id),
            "universal-agents.io/job-id": self._label_value(attempt.job_id),
            "universal-agents.io/network": "enabled" if profile.network_enabled else "disabled",
        }
        input_path = f"/ua/input/{revision.entrypoint}"
        runtime_command = (
            [profile.runtime_executable, "-I", "-B", input_path, *revision.args]
            if revision.language == ExecutionLanguage.PYTHON
            else [input_path, *revision.args]
        )
        workload_env = [
            {"name": key, "value": value}
            for key, value in sorted(revision.non_secret_env.items())
        ]
        workload_mounts = [
            {"name": "input", "mountPath": "/ua/input", "readOnly": True}
        ]
        volumes = [
            {"name": "input", "emptyDir": {"sizeLimit": "64Mi"}},
            {
                "name": "artifact-store",
                "persistentVolumeClaim": {
                    "claimName": profile.kubernetes_artifact_pvc_name,
                },
            },
        ]
        if profile.kubernetes_workload_pvc_name:
            volumes.append(
                {
                    "name": "workload-data",
                    "persistentVolumeClaim": {
                        "claimName": profile.kubernetes_workload_pvc_name,
                    },
                }
            )
            workload_mounts.append({"name": "workload-data", "mountPath": "/ua/data"})
            workload_env.append({"name": "UA_DATA_DIR", "value": "/ua/data"})
        manifest: dict[str, Any] = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": profile.kubernetes_namespace,
                "labels": labels,
                "annotations": {
                    "universal-agents.io/revision-digest": revision.revision_digest,
                    "universal-agents.io/profile-digest": profile.profile_digest,
                    "universal-agents.io/fence": str(attempt.fence),
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": profile.limits.wall_time_seconds,
                "ttlSecondsAfterFinished": 86_400,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": profile.kubernetes_service_account_name,
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                        "volumes": volumes,
                        "initContainers": [
                            {
                                "name": "artifact-fetch",
                                "image": profile.runtime_image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    profile.artifact_fetcher_executable,
                                    "--source",
                                    f"/ua/artifacts/{revision.build.output.storage_key}",
                                    "--sha256",
                                    revision.build.output.sha256,
                                    "--output",
                                    input_path,
                                ],
                                "securityContext": self._container_security_context(),
                                "volumeMounts": [
                                    {"name": "input", "mountPath": "/ua/input"},
                                    {
                                        "name": "artifact-store",
                                        "mountPath": "/ua/artifacts",
                                        "readOnly": True,
                                    },
                                ],
                            }
                        ],
                        "containers": [
                            {
                                "name": "workload",
                                "image": profile.runtime_image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": runtime_command,
                                "env": workload_env,
                                "resources": {
                                    "limits": {
                                        "memory": str(profile.limits.memory_bytes),
                                        "ephemeral-storage": str(profile.limits.writable_bytes),
                                    }
                                },
                                "securityContext": self._container_security_context(),
                                "volumeMounts": workload_mounts,
                            }
                        ],
                    },
                },
            },
        }
        manifest["metadata"]["annotations"]["universal-agents.io/spec-digest"] = (
            self.spec_digest(manifest)
        )
        return manifest

    @staticmethod
    def spec_digest(manifest: dict[str, Any]) -> str:
        payload = json.loads(json.dumps(manifest))
        payload.get("metadata", {}).get("annotations", {}).pop(
            "universal-agents.io/spec-digest", None
        )
        return contract_sha256(payload)

    @staticmethod
    def job_name(run_id: str, fence: int) -> str:
        normalized = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
        suffix = hashlib.sha256(f"{run_id}:{fence}".encode()).hexdigest()[:10]
        prefix = normalized[: 63 - len(suffix) - 4]
        return f"ua-{prefix}-{suffix}".strip("-")

    @staticmethod
    def _label_value(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-_.")
        return (normalized or "unknown")[:63]

    @staticmethod
    def _container_security_context() -> dict[str, Any]:
        return {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
        }


@dataclass(frozen=True)
class KubernetesBatchExecutionAdapter:
    artifact_store: ArtifactStore
    client: KubernetesJobClient
    renderer: KubernetesJobRenderer
    poll_interval_seconds: float = 2.0

    async def execute(
        self,
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        attempt: RunAttemptV1,
        output_callback: OutputCallback | None = None,
        started_callback: StartedCallback | None = None,
    ) -> BatchExecutionResult:
        manifest = self.renderer.render(profile=profile, revision=revision, attempt=attempt)
        expected_name = str(manifest["metadata"]["name"])
        expected_digest = str(
            manifest["metadata"]["annotations"]["universal-agents.io/spec-digest"]
        )
        namespace = str(profile.kubernetes_namespace)
        state, active_digest = await self._start_or_adopt(
            attempt=attempt,
            manifest=manifest,
            namespace=namespace,
            expected_name=expected_name,
            expected_digest=expected_digest,
        )
        if started_callback is not None:
            try:
                await started_callback(state.name, state.uid, active_digest)
            except Exception as exc:
                raise KubernetesOutcomeUnknown(
                    "Kubernetes Job started but its exact identity could not be recorded."
                ) from exc
        while state.active:
            await asyncio.sleep(self.poll_interval_seconds)
            refreshed = await self.client.get_job(namespace=namespace, name=state.name)
            if refreshed is None or refreshed.uid != state.uid:
                raise KubernetesOutcomeUnknown(
                    "Kubernetes Job disappeared or changed UID before terminal observation."
                )
            self._require_digest(refreshed, active_digest)
            state = refreshed
        logs = await self.client.read_logs(
            namespace=namespace,
            name=state.name,
            maximum_bytes=profile.limits.artifact_bytes,
        )
        if output_callback is not None and logs:
            await output_callback("stdout", logs)
        log_artifact = self.artifact_store.put_bytes(
            kind=ArtifactKind.LOG,
            payload=logs[: profile.limits.artifact_bytes],
            media_type="text/plain",
        )
        return BatchExecutionResult(
            exit_code=state.exit_code if state.exit_code is not None else (0 if state.succeeded else 1),
            stdout_preview=logs[: profile.limits.stdout_bytes].decode("utf-8", errors="replace"),
            stderr_preview="",
            log_artifact=log_artifact,
            timed_out=False,
            backend_workload_id=state.name,
            backend_workload_uid=state.uid,
        )

    async def _start_or_adopt(
        self,
        *,
        attempt: RunAttemptV1,
        manifest: dict[str, Any],
        namespace: str,
        expected_name: str,
        expected_digest: str,
    ) -> tuple[KubernetesJobState, str]:
        if attempt.backend_workload_id:
            existing = await self.client.get_job(
                namespace=namespace,
                name=attempt.backend_workload_id,
            )
            if existing is None or existing.uid != attempt.backend_workload_uid:
                raise KubernetesOutcomeUnknown(
                    "Previously recorded Kubernetes workload cannot be identified exactly."
                )
            if not attempt.backend_spec_digest:
                raise KubernetesOutcomeUnknown(
                    "Previously recorded Kubernetes workload lacks its trusted spec digest."
                )
            self._require_digest(existing, attempt.backend_spec_digest)
            return existing, attempt.backend_spec_digest
        state: KubernetesJobState | None
        try:
            state = await self.client.create_job(manifest)
        except Exception as create_error:
            try:
                state = await self.client.get_job(
                    namespace=namespace,
                    name=expected_name,
                )
            except Exception as lookup_error:
                raise KubernetesOutcomeUnknown(
                    "Kubernetes Job creation outcome could not be determined."
                ) from lookup_error
            if state is None:
                raise KubernetesOutcomeUnknown(
                    "Kubernetes Job creation failed, but absence after an ambiguous API "
                    "result is not proof that no workload ran."
                ) from create_error
        if state is None:
            raise KubernetesOutcomeUnknown(
                "Kubernetes Job creation returned no identifiable workload."
            )
        if state.name != expected_name:
            raise KubernetesOutcomeUnknown("Kubernetes API returned an unexpected Job name.")
        self._require_digest(state, expected_digest)
        return state, expected_digest

    @staticmethod
    def _require_digest(state: KubernetesJobState, expected_digest: str) -> None:
        if state.spec_digest != expected_digest:
            raise KubernetesOutcomeUnknown("Kubernetes Job spec digest does not match the attempt.")


@dataclass(frozen=True)
class KubectlJobClient:
    """Dependency-free Kubernetes client using an operator-installed kubectl binary."""

    executable: str = "kubectl"
    kubeconfig_path: Path | None = None
    request_timeout_seconds: int = 30
    maximum_response_bytes: int = 16_777_216

    async def create_job(self, manifest: dict[str, Any]) -> KubernetesJobState:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
            handle.flush()
            payload = await self._run("create", "-f", handle.name, "-o", "json")
        return self._state(json.loads(payload))

    async def get_job(self, *, namespace: str, name: str) -> KubernetesJobState | None:
        try:
            payload = await self._run("get", "job", name, "-n", namespace, "-o", "json")
        except ExecutionAdapterError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc):
                return None
            raise
        return self._state(json.loads(payload))

    async def read_logs(
        self,
        *,
        namespace: str,
        name: str,
        maximum_bytes: int,
    ) -> bytes:
        payload = await self._run(
            "logs",
            f"job/{name}",
            "-n",
            namespace,
            "--limit-bytes",
            str(maximum_bytes),
            maximum_stdout_bytes=maximum_bytes,
        )
        return payload.encode("utf-8")

    async def _run(
        self,
        *arguments: str,
        maximum_stdout_bytes: int | None = None,
    ) -> str:
        argv = [self.executable]
        if self.kubeconfig_path is not None:
            argv.extend(["--kubeconfig", str(self.kubeconfig_path)])
        argv.extend(arguments)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def read_bounded(
            stream: asyncio.StreamReader | None,
            limit: int,
        ) -> bytes:
            captured = bytearray()
            if stream is None:
                return b""
            while chunk := await stream.read(65_536):
                if len(captured) < limit:
                    captured.extend(chunk[: limit - len(captured)])
            return bytes(captured)

        stdout_limit = max(
            1,
            min(
                int(maximum_stdout_bytes or self.maximum_response_bytes),
                self.maximum_response_bytes,
            ),
        )
        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    read_bounded(process.stdout, stdout_limit),
                    read_bounded(process.stderr, 65_536),
                    process.wait(),
                ),
                timeout=self.request_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ExecutionAdapterError("kubectl request timed out.") from exc
        if process.returncode != 0:
            raise ExecutionAdapterError(
                f"kubectl failed with exit {process.returncode}: "
                f"{stderr.decode('utf-8', errors='replace')[:2000]}"
            )
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _state(payload: dict[str, Any]) -> KubernetesJobState:
        metadata = payload.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        status = payload.get("status") or {}
        conditions = {item.get("type"): item.get("status") for item in status.get("conditions") or []}
        succeeded = conditions.get("Complete") == "True" or int(status.get("succeeded") or 0) > 0
        failed = conditions.get("Failed") == "True" or int(status.get("failed") or 0) > 0
        return KubernetesJobState(
            name=str(metadata.get("name") or ""),
            uid=str(metadata.get("uid") or ""),
            spec_digest=str(annotations.get("universal-agents.io/spec-digest") or ""),
            active=not succeeded and not failed,
            succeeded=succeeded,
            failed=failed,
            exit_code=None,
        )


@dataclass(frozen=True)
class KubernetesDeploymentState:
    name: str
    uid: str
    spec_digest: str
    replicas: int
    available_replicas: int


class KubernetesResourceClient(Protocol):
    async def apply(self, manifest: dict[str, Any]) -> dict[str, Any]: ...
    async def delete(self, *, kind: str, namespace: str, name: str) -> None: ...
    async def get_deployment(
        self, *, namespace: str, name: str
    ) -> KubernetesDeploymentState | None: ...


@dataclass(frozen=True)
class KubernetesManagedServiceRenderer:
    network_policy_enforced: bool

    def render(
        self,
        *,
        service: ManagedServiceV1,
        revision: ExecutionRevisionV1,
        profile: ExecutionProfileV1,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if profile.backend != ExecutionBackendKind.KUBERNETES:
            raise ExecutionAdapterError("Managed Kubernetes services require a Kubernetes profile.")
        if service.restart_policy != ServiceRestartPolicy.ALWAYS:
            raise ExecutionAdapterError(
                "Kubernetes Deployments support only the managed-service restart policy 'always'."
            )
        if not profile.network_enabled and not self.network_policy_enforced:
            raise ExecutionAdapterError(
                "A network-disabled managed service requires an enforced NetworkPolicy."
            )
        if revision.build.output.storage_backend != ArtifactStorageBackend.FILESYSTEM:
            raise ExecutionAdapterError(
                "Managed Kubernetes PVC transport requires a filesystem artifact reference."
            )
        name = self.deployment_name(service.service_id)
        labels = {
            "app.kubernetes.io/managed-by": "universal-agents",
            "universal-agents.io/service-id": KubernetesJobRenderer._label_value(
                service.service_id
            ),
            "universal-agents.io/network": "enabled" if profile.network_enabled else "disabled",
        }
        input_path = f"/ua/input/{revision.entrypoint}"
        command = (
            [profile.runtime_executable, "-I", "-B", input_path, *revision.args]
            if revision.language == ExecutionLanguage.PYTHON
            else [input_path, *revision.args]
        )
        workload_env = [
            {"name": key, "value": value}
            for key, value in sorted(revision.non_secret_env.items())
        ]
        workload_mounts = [
            {"name": "input", "mountPath": "/ua/input", "readOnly": True}
        ]
        volumes = [
            {"name": "input", "emptyDir": {"sizeLimit": "64Mi"}},
            {
                "name": "artifact-store",
                "persistentVolumeClaim": {
                    "claimName": profile.kubernetes_artifact_pvc_name,
                },
            },
        ]
        if profile.kubernetes_workload_pvc_name:
            volumes.append(
                {
                    "name": "workload-data",
                    "persistentVolumeClaim": {
                        "claimName": profile.kubernetes_workload_pvc_name,
                    },
                }
            )
            workload_mounts.append({"name": "workload-data", "mountPath": "/ua/data"})
            workload_env.append({"name": "UA_DATA_DIR", "value": "/ua/data"})
        replicas = 1 if service.desired_state == ServiceDesiredState.RUNNING else 0
        pod_spec: dict[str, Any] = {
            "serviceAccountName": profile.kubernetes_service_account_name,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 15,
            "securityContext": {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumes": volumes,
            "initContainers": [
                {
                    "name": "artifact-fetch",
                    "image": profile.runtime_image,
                    "command": [
                        profile.artifact_fetcher_executable,
                        "--source",
                        f"/ua/artifacts/{revision.build.output.storage_key}",
                        "--sha256",
                        revision.build.output.sha256,
                        "--output",
                        input_path,
                    ],
                    "securityContext": KubernetesJobRenderer._container_security_context(),
                    "volumeMounts": [
                        {"name": "input", "mountPath": "/ua/input"},
                        {
                            "name": "artifact-store",
                            "mountPath": "/ua/artifacts",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            "containers": [
                {
                    "name": "workload",
                    "image": profile.runtime_image,
                    "command": command,
                    "env": workload_env,
                    "ports": (
                        [{"name": "mcp", "containerPort": service.port}]
                        if service.service_kind == ServiceKind.MCP_SERVER
                        else []
                    ),
                    "resources": {
                        "limits": {
                            "memory": str(profile.limits.memory_bytes),
                            "ephemeral-storage": str(profile.limits.writable_bytes),
                        }
                    },
                    "securityContext": KubernetesJobRenderer._container_security_context(),
                    "volumeMounts": workload_mounts,
                }
            ],
        }
        if service.service_kind == ServiceKind.MCP_SERVER:
            probe_period = max(1, min(service.health_check_interval_seconds, 30))
            workload = pod_spec["containers"][0]
            workload["readinessProbe"] = {
                "tcpSocket": {"port": "mcp"},
                "periodSeconds": probe_period,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
            }
            workload["startupProbe"] = {
                "tcpSocket": {"port": "mcp"},
                "periodSeconds": probe_period,
                "timeoutSeconds": 1,
                "failureThreshold": max(
                    1,
                    math.ceil(service.startup_timeout_seconds / probe_period),
                ),
            }
        deployment: dict[str, Any] = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": profile.kubernetes_namespace,
                "labels": labels,
                "annotations": {
                    "universal-agents.io/revision-digest": revision.revision_digest,
                    "universal-agents.io/profile-digest": profile.profile_digest,
                    "universal-agents.io/generation": str(service.generation),
                },
            },
            "spec": {
                "replicas": replicas,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }
        deployment["metadata"]["annotations"]["universal-agents.io/spec-digest"] = (
            KubernetesJobRenderer.spec_digest(deployment)
        )
        cluster_service = None
        if service.service_kind == ServiceKind.MCP_SERVER:
            cluster_service = {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": name,
                    "namespace": profile.kubernetes_namespace,
                    "labels": labels,
                },
                "spec": {
                    "type": "ClusterIP",
                    "selector": labels,
                    "ports": [{"name": "mcp", "port": service.port, "targetPort": "mcp"}],
                },
            }
        return deployment, cluster_service

    @staticmethod
    def deployment_name(service_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9-]+", "-", service_id.lower()).strip("-")
        suffix = hashlib.sha256(service_id.encode()).hexdigest()[:10]
        return f"ua-{normalized[:49]}-{suffix}"[:63].strip("-")


@dataclass(frozen=True)
class KubernetesManagedServiceBackend:
    client: KubernetesResourceClient
    renderer: KubernetesManagedServiceRenderer

    async def reconcile(
        self,
        *,
        service: ManagedServiceV1,
        revision: ExecutionRevisionV1,
        profile: ExecutionProfileV1,
    ) -> ServiceBackendObservation:
        name = self.renderer.deployment_name(service.service_id)
        namespace = str(profile.kubernetes_namespace)
        if service.desired_state == ServiceDesiredState.REMOVED:
            await self.client.delete(
                kind="deployment",
                namespace=namespace,
                name=name,
            )
            if service.service_kind == ServiceKind.MCP_SERVER:
                await self.client.delete(
                    kind="service",
                    namespace=namespace,
                    name=name,
                )
            return ServiceBackendObservation(observed_state=ServiceObservedState.STOPPED)
        deployment, cluster_service = self.renderer.render(
            service=service,
            revision=revision,
            profile=profile,
        )
        if cluster_service is not None:
            await self.client.apply(cluster_service)
        applied = await self.client.apply(deployment)
        metadata = applied.get("metadata") or {}
        expected_digest = deployment["metadata"]["annotations"][
            "universal-agents.io/spec-digest"
        ]
        actual_digest = (metadata.get("annotations") or {}).get(
            "universal-agents.io/spec-digest"
        )
        if actual_digest != expected_digest:
            raise KubernetesOutcomeUnknown("Applied Deployment spec digest changed unexpectedly.")
        name = str(metadata.get("name") or deployment["metadata"]["name"])
        uid = str(metadata.get("uid") or "")
        if service.desired_state != ServiceDesiredState.RUNNING:
            return ServiceBackendObservation(
                observed_state=ServiceObservedState.STOPPED,
                workload_id=name,
                workload_uid=uid or None,
            )
        observed = await self.client.get_deployment(
            namespace=str(profile.kubernetes_namespace),
            name=name,
        )
        if observed is None or observed.uid != uid or observed.spec_digest != expected_digest:
            raise KubernetesOutcomeUnknown("Managed Deployment identity cannot be verified.")
        if observed.available_replicas < 1:
            return ServiceBackendObservation(
                observed_state=ServiceObservedState.STARTING,
                workload_id=name,
                workload_uid=uid,
            )
        return ServiceBackendObservation(
            observed_state=ServiceObservedState.RUNNING,
            workload_id=name,
            workload_uid=uid,
            healthy_at_utc=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class KubectlResourceClient:
    kubectl: KubectlJobClient

    async def apply(self, manifest: dict[str, Any]) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
            handle.flush()
            payload = await self.kubectl._run("apply", "-f", handle.name, "-o", "json")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ExecutionAdapterError("kubectl apply returned a non-object response.")
        return decoded

    async def delete(self, *, kind: str, namespace: str, name: str) -> None:
        await self.kubectl._run(
            "delete",
            kind,
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
        )

    async def get_deployment(
        self, *, namespace: str, name: str
    ) -> KubernetesDeploymentState | None:
        try:
            payload = await self.kubectl._run(
                "get", "deployment", name, "-n", namespace, "-o", "json"
            )
        except ExecutionAdapterError as exc:
            if "NotFound" in str(exc) or "not found" in str(exc):
                return None
            raise
        record = json.loads(payload)
        metadata = record.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        spec = record.get("spec") or {}
        status = record.get("status") or {}
        return KubernetesDeploymentState(
            name=str(metadata.get("name") or ""),
            uid=str(metadata.get("uid") or ""),
            spec_digest=str(annotations.get("universal-agents.io/spec-digest") or ""),
            replicas=int(spec.get("replicas") or 0),
            available_replicas=int(status.get("availableReplicas") or 0),
        )


__all__ = [
    "KubectlJobClient",
    "KubernetesBatchExecutionAdapter",
    "KubernetesJobClient",
    "KubernetesJobRenderer",
    "KubernetesJobState",
    "KubernetesDeploymentState",
    "KubernetesManagedServiceBackend",
    "KubernetesManagedServiceRenderer",
    "KubernetesOutcomeUnknown",
    "KubernetesResourceClient",
    "KubectlResourceClient",
]
