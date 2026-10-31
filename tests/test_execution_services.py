from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest
from strands.interrupt import InterruptException

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from custom_agents.execution_control_service import ExecutionControlService
from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_tools.providers.execution_control import (
    plan_managed_execution_service,
    plan_managed_execution_service_state,
)
from tests.task_work_support import set_agent_ref
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionSource,
    MCPConnectionStore,
    resolve_mcp_connection_paths,
)
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
    BuildAttestationV1,
    ExecutionActivationKind,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    ManagedServiceV1,
    OwnerScopeKind,
    OwnerScopeV1,
    ServiceDesiredState,
    ServiceKind,
    ServiceObservedState,
    ServiceRestartPolicy,
)
from runtime_hosting.kubernetes import (
    KubernetesManagedServiceRenderer,
)
from runtime_hosting.mcp_services import ManagedMCPServicePublisher
from runtime_hosting.profiles import ExecutionProfileRegistry
from runtime_hosting.services import ManagedServiceReconciler, ServiceBackendObservation
from runtime_hosting.controller_service import build_execution_access_guard
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.change_approval_support import (
    CEDAR_BINARY,
    ToolCall,
    decide,
    needs_cedar,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)
from tests.execution_access_support import (
    KUBERNETES_PROFILE_ID,
    activation as build_activation,
    execution_instance,
    execution_lead,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_AGREEMENT_GUARD = ExecutionAccessGuard(authorization_enabled=False)


def _owner() -> OwnerScopeV1:
    return OwnerScopeV1(
        scope_kind=OwnerScopeKind.TEAM,
        owner_id="team_alpha",
        team_id="team_alpha",
    )


def _profile() -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id="python.kubernetes.service",
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.KUBERNETES,
        runtime_executable="/usr/local/bin/python",
        toolchain_identity="python-test",
        runtime_image=f"registry.example.test/worker@sha256:{_SHA_A}",
        artifact_fetcher_executable="/usr/local/bin/ua-artifact-fetch",
        kubernetes_artifact_pvc_name="ua-execution-artifacts",
        kubernetes_workload_pvc_name="ua-execution-data",
        kubernetes_namespace="ua-execution",
        kubernetes_service_account_name="ua-worker",
        network_enabled=True,
    )


def _revision(profile: ExecutionProfileV1) -> ExecutionRevisionV1:
    build = BuildAttestationV1.build(
        owner=_owner(),
        source_artifact_id="exec_code_service",
        source_sha256=_SHA_A,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        toolchain_identity=profile.toolchain_identity,
        builder_identity="builder:test",
        output=ArtifactReferenceV1(
            kind=ArtifactKind.BUILD,
            sha256=_SHA_B,
            size_bytes=10,
            media_type="application/vnd.ua.python-source",
            storage_backend=ArtifactStorageBackend.FILESYSTEM,
            storage_key=f"build/bb/{_SHA_B}",
        ),
        entrypoint="server.py",
    )
    return ExecutionRevisionV1.build_revision(
        owner=_owner(),
        language=ExecutionLanguage.PYTHON,
        build=build,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        entrypoint=build.entrypoint,
    )


def _service(
    revision: ExecutionRevisionV1,
    *,
    expires_at: datetime | None = None,
    service_kind: ServiceKind = ServiceKind.PROCESS,
    canonical_user_id: str | None = None,
    service_id: str = "exec_svc_test",
    desired_state: ServiceDesiredState = ServiceDesiredState.RUNNING,
    mode: ChangeApprovalMode = ChangeApprovalMode.AGREEMENT,
) -> ManagedServiceV1:
    staged = ManagedServiceV1(
        service_id=service_id,
        name="Managed server",
        service_kind=service_kind,
        owner=_owner(),
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        desired_state=ServiceDesiredState.STOPPED,
        restart_policy=ServiceRestartPolicy.ALWAYS,
        port=8765 if service_kind == ServiceKind.MCP_SERVER else None,
        mcp_tool_names=("lookup",) if service_kind == ServiceKind.MCP_SERVER else (),
        mcp_target_agent_types=("Researcher",) if service_kind == ServiceKind.MCP_SERVER else (),
    )
    candidate = staged.model_copy(update={"desired_state": desired_state})
    activation = build_activation(
        ExecutionActivationKind.SERVICE,
        resource_id=staged.service_id,
        resource_digest=candidate.activation_resource_digest(),
        generation=staged.generation,
        owner_scope=staged.owner,
        mode=mode,
        person_id=canonical_user_id,
        function="change:managed_execution_service",
        activated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        expires_at=expires_at,
    )
    return ManagedServiceV1.model_validate(
        {
            **candidate.model_dump(mode="python"),
            "activation": activation,
        }
    )


class _Backend:
    def __init__(self) -> None:
        self.desired_states: list[ServiceDesiredState] = []

    async def reconcile(self, *, service: ManagedServiceV1, **_kwargs: Any) -> ServiceBackendObservation:
        self.desired_states.append(service.desired_state)
        state = (
            ServiceObservedState.RUNNING
            if service.desired_state == ServiceDesiredState.RUNNING
            else ServiceObservedState.STOPPED
        )
        return ServiceBackendObservation(
            observed_state=state,
            workload_id="deployment-1",
            workload_uid="uid-1",
            healthy_at_utc=datetime.now(timezone.utc) if state == ServiceObservedState.RUNNING else None,
        )


def test_managed_mcp_renderer_creates_cluster_service_and_recreate_deployment() -> None:
    profile = _profile()
    revision = _revision(profile)
    service = _service(revision, service_kind=ServiceKind.MCP_SERVER).model_copy(
        update={"active_fence": 9}
    )

    deployment, cluster_service = KubernetesManagedServiceRenderer(
        network_policy_enforced=True
    ).render(service=service, revision=revision, profile=profile)

    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert deployment["spec"]["replicas"] == 1
    assert "universal-agents.io/fence" not in deployment["metadata"]["annotations"]
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert {mount["name"] for mount in pod_spec["containers"][0]["volumeMounts"]} == {
        "input",
        "workload-data",
    }
    assert cluster_service is not None
    assert cluster_service["spec"]["ports"][0]["port"] == service.port
    assert pod_spec["containers"][0]["readinessProbe"]["tcpSocket"]["port"] == "mcp"
    assert pod_spec["containers"][0]["startupProbe"]["tcpSocket"]["port"] == "mcp"


def test_managed_mcp_publication_stays_pending_for_separate_activation(
    tmp_path: Path,
) -> None:
    profile = _profile()
    revision = _revision(profile)
    service = _service(
        revision,
        service_kind=ServiceKind.MCP_SERVER,
        canonical_user_id="user_alice",
    )
    store = MCPConnectionStore(resolve_mcp_connection_paths(tmp_path))
    publisher = ManagedMCPServicePublisher(
        connection_store=store,
        endpoint_resolver=lambda _service: "http://managed.ua.svc:8765/mcp",
    )

    publisher.publish(service)
    publisher.publish(service)

    records = store.list_records()
    assert len(records) == 1
    assert records[0].lifecycle == MCPConnectionLifecycle.PENDING
    assert records[0].descriptor.source == MCPConnectionSource.HOSTED
    assert records[0].target_agent_types == ("Researcher",)


@pytest.mark.asyncio
async def test_a_retired_service_without_an_approval_is_stopped(tmp_path: Path) -> None:
    """Records retired by the operator keep no approval; stopping needs none."""

    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    profile = _profile()
    revision = _revision(profile)
    repository.put_build_attestation(revision.build)
    repository.put_revision(revision)
    repository.create_managed_service(
        ManagedServiceV1(
            service_id="exec_svc_retired",
            name="Retired service",
            owner=_owner(),
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            desired_state=ServiceDesiredState.STOPPED,
            backend_workload_id="ua-exec-retired",
        )
    )
    backend = _Backend()
    reconciler = ManagedServiceReconciler(
        repository=repository,
        profiles=ExecutionProfileRegistry((profile,)),
        authorization_guard=_AGREEMENT_GUARD,
        backends={ExecutionBackendKind.KUBERNETES.value: backend},
        team_id="team_alpha",
        reconciler_id="reconciler-a",
    )

    observed = await reconciler.reconcile_once()

    assert observed is not None and observed.observed_state == ServiceObservedState.STOPPED
    assert backend.desired_states == [ServiceDesiredState.STOPPED]


@needs_cedar
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ChangeApprovalMode.PERSON_APPROVAL, ServiceObservedState.RUNNING),
        (ChangeApprovalMode.AGREEMENT, ServiceObservedState.AUTHORIZATION_EXPIRED),
    ],
)
async def test_with_authorization_on_a_persons_approval_runs_a_service_and_agreement_does_not(
    tmp_path: Path, mode: ChangeApprovalMode, expected: ServiceObservedState
) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    profile = _profile()
    revision = _revision(profile)
    repository.put_build_attestation(revision.build)
    repository.put_revision(revision)
    repository.create_managed_service(_service(revision, canonical_user_id="user_alice", mode=mode))
    backend = _Backend()
    catalogs = CatalogSource(None)
    access = AccessService(
        store=AccessRepository(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        ),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id="team_alpha",
    )
    reconciler = ManagedServiceReconciler(
        repository=repository,
        profiles=ExecutionProfileRegistry((profile,)),
        authorization_guard=ExecutionAccessGuard(authorization_enabled=True, access=access, catalogs=catalogs),
        backends={ExecutionBackendKind.KUBERNETES.value: backend},
        team_id="team_alpha",
        reconciler_id="reconciler-a",
    )

    try:
        observed = await reconciler.reconcile_once()
    finally:
        access.cedar.close()

    assert observed is not None and observed.observed_state == expected
    if expected is ServiceObservedState.AUTHORIZATION_EXPIRED:
        assert backend.desired_states == [ServiceDesiredState.STOPPED], "the workload is stopped"
        assert "Authorization is on now" in (observed.last_error_message or "")


# -- the managed_execution_service change, end to end ---------------------------


@pytest.fixture
def lead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    use_signed_contexts(monkeypatch)
    instance = execution_instance(tmp_path, authorization_enabled=getattr(request, "param", True))
    agent = execution_lead(instance)
    agent.instance = instance
    set_agent_ref(agent)
    yield agent
    set_agent_ref(None)


def _kubernetes_revision(agent: Any) -> str:
    return (
        ExecutionControlService(agent)
        .promote_revision(
            source="print('service')\n",
            source_name="server.py",
            language="python",
            profile_id=KUBERNETES_PROFILE_ID,
        )
        .revision_id
    )


def _plan_service(agent: Any, **extra: Any) -> dict[str, Any]:
    staged = json.loads(
        plan_managed_execution_service._tool_func(
            name="report-server",
            revision_id=_kubernetes_revision(agent),
            reason="Serve the nightly reports",
            **extra,
        )
    )
    assert staged.get("status") == "staged", staged
    return staged


def _approved(agent: Any, change_id: str, tool_use_id: str) -> dict[str, Any]:
    """Pause for review, let the owner approve, resume (one tool call id per apply_change call)."""

    call = ToolCall(tool_use_id)
    with pytest.raises(InterruptException):
        run_apply_change(agent, change_id, call)
    decide(agent, call)
    return run_apply_change(agent, change_id, call)


async def _reconcile(agent: Any) -> ManagedServiceV1 | None:
    execution = ExecutionControlService(agent)
    guard = build_execution_access_guard(agent.instance.config)
    try:
        return await ManagedServiceReconciler(
            repository=execution.repository,
            profiles=execution.profiles,
            authorization_guard=guard,
            backends={ExecutionBackendKind.KUBERNETES.value: _Backend()},
            team_id=agent.team_id,
            reconciler_id="reconciler-test",
        ).reconcile_once()
    finally:
        if guard.access is not None:
            guard.access.cedar.close()


@needs_cedar
def test_a_managed_service_waits_for_a_person_then_runs_and_cannot_be_replayed(lead: Any) -> None:
    staged = _plan_service(lead)
    assert staged["change_id"].startswith("exec_svc_change_")
    assert any("Kubernetes" in line for line in staged["effect"])
    assert any("blocks network access" in line for line in staged["effect"])
    call = ToolCall()

    with pytest.raises(InterruptException):
        run_apply_change(lead, staged["change_id"], call)
    assert ExecutionControlService(lead).list_services() == (), "nothing exists before approval"
    request = stored_request(lead, call)
    assert request.change is not None and request.change.kind == "managed_execution_service"
    decide(lead, call)
    applied = run_apply_change(lead, staged["change_id"], call)

    assert applied["status"] == "applied", applied
    (service,) = ExecutionControlService(lead).list_services()
    activation = service.activation
    assert activation is not None and service.desired_state == ServiceDesiredState.RUNNING
    assert activation.operation == "fn:execution_control#change:managed_execution_service"
    assert activation.approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert activation.approval.access_request_id == request.request_id
    assert activation.resource_digest == service.activation_resource_digest()
    observed = asyncio.run(_reconcile(lead))
    assert observed is not None and observed.observed_state == ServiceObservedState.RUNNING

    replay = run_apply_change(lead, staged["change_id"], call)
    assert replay["status"] == "refused" and "already applied" in replay["reason"]


@needs_cedar
def test_a_tampered_or_stale_service_plan_is_refused_before_anyone_is_asked(lead: Any) -> None:
    staged = _plan_service(lead)
    plan_path = (
        lead.instance.root / "runtime" / "execution-changes" / "managed-services" / f"{staged['change_id']}.json"
    )
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["desired_state"] = "stopped"
    plan_path.chmod(0o600)
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    tampered_call = ToolCall("tooluse_tampered")

    tampered = run_apply_change(lead, staged["change_id"], tampered_call)

    assert tampered["status"] == "refused" and "changed after staging" in tampered["reason"]
    assert tampered_call.reasons == [], "no person was asked"

    fresh = _plan_service(lead)
    created = _approved(lead, fresh["change_id"], "tooluse_create")
    assert created["status"] == "applied", created
    service_id = created["result"]["service"]["service_id"]
    stop = json.loads(
        plan_managed_execution_service_state._tool_func(
            service_id=service_id, desired_state="stopped", reason="Pause the reports"
        )
    )
    remove = json.loads(
        plan_managed_execution_service_state._tool_func(
            service_id=service_id, desired_state="removed", reason="Retire the reports"
        )
    )
    stopped = _approved(lead, stop["change_id"], "tooluse_stop")
    assert stopped["status"] == "applied", stopped
    stale_call = ToolCall("tooluse_stale")

    stale = run_apply_change(lead, remove["change_id"], stale_call)

    assert stale["status"] == "refused" and "changed after planning" in stale["reason"]
    assert stale_call.reasons == []
    (service,) = ExecutionControlService(lead).list_services()
    assert service.desired_state == ServiceDesiredState.STOPPED
    assert service.activation is not None and service.activation.resource_digest == service.activation_resource_digest()


@pytest.mark.parametrize("lead", [False], indirect=True)
def test_with_authorization_off_the_persons_agreement_applies_a_service(lead: Any) -> None:
    staged = _plan_service(lead)

    needs_agreement = run_apply_change(lead, staged["change_id"])
    applied = run_apply_change(lead, staged["change_id"], confirmed=True)

    assert needs_agreement["status"] == "agreement_required"
    assert applied["status"] == "applied", applied
    (service,) = ExecutionControlService(lead).list_services()
    assert service.activation is not None and service.activation.approval.mode is ChangeApprovalMode.AGREEMENT
    observed = asyncio.run(_reconcile(lead))
    assert observed is not None and observed.observed_state == ServiceObservedState.RUNNING
