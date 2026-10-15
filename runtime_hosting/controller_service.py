"""Managed runtime loop for schedules, finite attempts, and long-lived services."""

from __future__ import annotations

import logging

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import agent_role_resolver
from assistant_gateway.config import GatewayInstanceConfig
from mcp_control.registry import (
    MCPConnectionStore,
    resolve_mcp_connection_paths,
)
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.controller_loop import supervise_execution_controller
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_adapters import (
    BatchExecutionAdapter,
    LocalBatchExecutionAdapter,
    OpenShellBatchExecutionAdapter,
)
from runtime_hosting.execution_contracts import ExecutionBackendKind, ManagedServiceV1
from runtime_hosting.kubernetes import (
    KubectlJobClient,
    KubectlResourceClient,
    KubernetesBatchExecutionAdapter,
    KubernetesJobRenderer,
    KubernetesManagedServiceBackend,
    KubernetesManagedServiceRenderer,
)
from runtime_hosting.mcp_services import (
    ManagedMCPServicePublisher,
    kubernetes_mcp_endpoint,
)
from runtime_hosting.profiles import ExecutionProfileRegistry
from runtime_hosting.runner import BatchRunController
from runtime_hosting.scheduling import ScheduleMaterializer
from runtime_hosting.services import ManagedServiceReconciler
from storage.access_repository import AccessRepository
from storage.execution_control_repository import create_execution_control_repository


def build_execution_access_guard(config: GatewayInstanceConfig) -> ExecutionAccessGuard:
    """The controller's access checks, built the way the broker builds its access gate.

    With authorization on it reads grants from the access store in the manager
    storage and decides with the real Cedar evaluator, against the current
    tool catalog (reloaded when instance tool files or MCP records change).
    """

    if not config.authorization.enabled:
        return ExecutionAccessGuard(authorization_enabled=False)
    catalogs = CatalogSource(config.instance_root_path)
    access = AccessService(
        store=AccessRepository(config.manager_storage_settings),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=config.authorization.cedar_binary_path),
        team_id=config.gateway.team_id,
        agent_role_of=agent_role_resolver(config.manager.named_agent_type),
        lead_agent_type=config.manager.named_agent_type,
    )
    return ExecutionAccessGuard(authorization_enabled=True, access=access, catalogs=catalogs)


async def run_execution_controller(
    config: GatewayInstanceConfig,
    *,
    logger: logging.Logger | None = None,
) -> None:
    log = logger or logging.getLogger(__name__)
    settings = config.execution_control
    if not settings.enabled or not settings.runner.enabled:
        log.warning("Execution controller is disabled by instance configuration.")
        return
    workspace = config.workspace_path
    artifact_store = FilesystemArtifactStore(
        config.layout.execution_artifacts_dir,
        maximum_artifact_bytes=settings.artifact_store.maximum_artifact_bytes,
    )
    run_root = config.layout.execution_runs_dir
    run_root.mkdir(parents=True, exist_ok=True)
    repository = create_execution_control_repository(
        config.resolve_manager_storage_settings(),
        logger=log,
    )
    old_records = repository.old_format_records(team_id=config.gateway.team_id)
    if old_records.present:
        message = old_records.message()
        log.error("%s event=execution_old_records_refused", message)
        raise RuntimeError(message)
    profiles = ExecutionProfileRegistry(settings.profiles)
    authorization_guard = build_execution_access_guard(config)
    kubectl = KubectlJobClient(executable=settings.kubernetes.kubectl_executable)
    adapters: dict[ExecutionBackendKind, BatchExecutionAdapter] = {
        ExecutionBackendKind.LOCAL: LocalBatchExecutionAdapter(
            artifact_store=artifact_store,
            run_root=run_root,
            logger=log,
        ),
        ExecutionBackendKind.OPENSHELL: OpenShellBatchExecutionAdapter(
            artifact_store=artifact_store,
            run_root=run_root,
            logger=log,
            host_workspace_root=workspace,
        ),
        ExecutionBackendKind.KUBERNETES: KubernetesBatchExecutionAdapter(
            artifact_store=artifact_store,
            client=kubectl,
            renderer=KubernetesJobRenderer(
                network_policy_enforced=settings.kubernetes.network_policy_enforced
            ),
        ),
    }
    runner = BatchRunController(
        repository=repository,
        profiles=profiles,
        authorization_guard=authorization_guard,
        adapters=adapters,
        team_id=config.gateway.team_id,
        runner_id=f"{config.instance_name}.execution-controller",
        lease_ttl_seconds=settings.runner.lease_ttl_seconds,
        logger=log,
    )
    materializer = ScheduleMaterializer(repository=repository, logger=log)
    resource_client = KubectlResourceClient(kubectl)

    def endpoint_for(managed: ManagedServiceV1) -> str:
        revision = repository.get_revision(
            team_id=managed.owner.team_id,
            revision_id=managed.revision_id,
        )
        if revision is None:
            raise RuntimeError("Managed MCP revision disappeared before publication.")
        profile = profiles.require(
            profile_id=revision.profile_id,
            profile_digest=revision.profile_digest,
        )
        if not profile.kubernetes_namespace:
            raise RuntimeError("Managed MCP publication currently requires Kubernetes hosting.")
        return kubernetes_mcp_endpoint(
            managed,
            namespace=profile.kubernetes_namespace,
        )

    publisher = ManagedMCPServicePublisher(
        connection_store=MCPConnectionStore(
            resolve_mcp_connection_paths(config.instance_root_path)
        ),
        endpoint_resolver=endpoint_for,
    )
    service_reconciler = ManagedServiceReconciler(
        repository=repository,
        profiles=profiles,
        authorization_guard=authorization_guard,
        backends={
            ExecutionBackendKind.KUBERNETES.value: KubernetesManagedServiceBackend(
                client=resource_client,
                renderer=KubernetesManagedServiceRenderer(
                    network_policy_enforced=settings.kubernetes.network_policy_enforced
                ),
            )
        },
        team_id=config.gateway.team_id,
        reconciler_id=f"{config.instance_name}.service-reconciler",
        lease_ttl_seconds=settings.runner.lease_ttl_seconds,
        publisher=publisher,
        logger=log,
    )
    async def materialize() -> None:
        materializer.materialize_due_jobs(
            team_id=config.gateway.team_id,
            limit=max(100, settings.runner.max_concurrent_jobs * 4),
        )

    try:
        await supervise_execution_controller(
            materialize=materialize,
            reconcile=service_reconciler.reconcile_once,
            run_once=runner.run_once,
            max_concurrent_jobs=settings.runner.max_concurrent_jobs,
            tick_interval=settings.runner.tick_interval_seconds,
            reconcile_interval=settings.runner.service_reconcile_interval_seconds,
        )
    finally:
        if authorization_guard.access is not None:
            authorization_guard.access.cedar.close()


__all__ = ["build_execution_access_guard", "run_execution_controller"]
