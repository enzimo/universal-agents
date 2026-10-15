"""Fenced reconciliation for long-lived process and executable MCP services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import secrets
from typing import Protocol

from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
)
from runtime_hosting.execution_contracts import (
    ExecutionProfileV1,
    ExecutionRevisionV1,
    ManagedServiceV1,
    ServiceDesiredState,
    ServiceObservedState,
)
from runtime_hosting.profiles import ExecutionProfileRegistry
from storage.execution_control_repository import ExecutionControlRepository, StaleExecutionFence


@dataclass(frozen=True)
class ServiceBackendObservation:
    observed_state: ServiceObservedState
    workload_id: str | None = None
    workload_uid: str | None = None
    healthy_at_utc: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class ManagedServiceBackend(Protocol):
    async def reconcile(
        self,
        *,
        service: ManagedServiceV1,
        revision: ExecutionRevisionV1,
        profile: ExecutionProfileV1,
    ) -> ServiceBackendObservation: ...


class ManagedServicePublisher(Protocol):
    def publish(self, service: ManagedServiceV1) -> None: ...
    def unpublish(self, service: ManagedServiceV1) -> None: ...


@dataclass
class ManagedServiceReconciler:
    repository: ExecutionControlRepository
    profiles: ExecutionProfileRegistry
    authorization_guard: ExecutionAccessGuard
    backends: dict[str, ManagedServiceBackend]
    team_id: str
    reconciler_id: str
    lease_ttl_seconds: int = 60
    publisher: ManagedServicePublisher | None = None
    logger: logging.Logger = logging.getLogger(__name__)

    async def reconcile_once(self) -> ManagedServiceV1 | None:
        lease_token = secrets.token_urlsafe(24)
        service = self.repository.claim_managed_service(
            team_id=self.team_id,
            reconciler_id=self.reconciler_id,
            lease_token=lease_token,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        if service is None:
            return None
        fence = service.active_fence
        revision = self.repository.get_revision(
            team_id=service.owner.team_id,
            revision_id=service.revision_id,
        )
        if revision is None:
            observation = ServiceBackendObservation(
                observed_state=ServiceObservedState.FAILED,
                error_code="revision_missing",
                error_message="The immutable service revision is unavailable.",
            )
        else:
            observation = await self._reconcile_backend(service, revision)
        updated = service.model_copy(
            update={
                "observed_state": observation.observed_state,
                "observed_generation": service.generation,
                "backend_workload_id": observation.workload_id,
                "backend_workload_uid": observation.workload_uid,
                "last_health_at_utc": observation.healthy_at_utc,
                "last_error_code": observation.error_code,
                "last_error_message": observation.error_message,
                "updated_at_utc": datetime.now(timezone.utc),
            }
        )
        try:
            finished = self.repository.finish_managed_service_reconciliation(
                updated,
                reconciler_id=self.reconciler_id,
                lease_token=lease_token,
                fence=fence,
            )
        except StaleExecutionFence as exc:
            # A newer desired state or another reconciler took over; the next
            # pass reconciles the current record. Not a controller failure.
            self.logger.warning(
                "Managed-service reconcile result discarded. event=managed_service_reconcile_stale "
                "service_id=%s generation=%s reason=%s",
                service.service_id,
                service.generation,
                exc,
            )
            return None
        if self.publisher is not None:
            if finished.observed_state == ServiceObservedState.RUNNING:
                self.publisher.publish(finished)
            else:
                self.publisher.unpublish(finished)
        return finished

    async def _reconcile_backend(
        self,
        service: ManagedServiceV1,
        revision: ExecutionRevisionV1,
    ) -> ServiceBackendObservation:
        try:
            if (
                service.desired_state != ServiceDesiredState.RUNNING
                and service.observed_generation == service.generation
                and service.observed_state == ServiceObservedState.STOPPED
            ):
                return ServiceBackendObservation(
                    observed_state=ServiceObservedState.STOPPED,
                    workload_id=service.backend_workload_id,
                    workload_uid=service.backend_workload_uid,
                )
            stopped_without_approval = (
                service.desired_state == ServiceDesiredState.STOPPED and service.activation is None
            )
            if stopped_without_approval and service.backend_workload_id is None:
                return ServiceBackendObservation(observed_state=ServiceObservedState.STOPPED)
            profile = self.profiles.require(
                profile_id=revision.profile_id,
                profile_digest=revision.profile_digest,
            )
            backend = self.backends[profile.backend.value]
            if stopped_without_approval:
                # A record retired by the operator (execution-retire-old-records)
                # keeps no approval. Stopping only reduces what runs, so the
                # controller stops its workload without one.
                return await backend.reconcile(service=service, revision=revision, profile=profile)
            if service.desired_state == ServiceDesiredState.RUNNING:
                try:
                    self.authorization_guard.require_service(service, revision)
                except ExecutionAuthorizationDenied as exc:
                    stopped = service.model_copy(
                        update={"desired_state": ServiceDesiredState.STOPPED}
                    )
                    stop_observation = await backend.reconcile(
                        service=stopped,
                        revision=revision,
                        profile=profile,
                    )
                    return ServiceBackendObservation(
                        observed_state=ServiceObservedState.AUTHORIZATION_EXPIRED,
                        workload_id=stop_observation.workload_id,
                        workload_uid=stop_observation.workload_uid,
                        error_code="service_authorization_denied",
                        error_message=str(exc),
                    )
            else:
                try:
                    self.authorization_guard.require_service(service, revision)
                except ExecutionAuthorizationDenied as exc:
                    return ServiceBackendObservation(
                        observed_state=ServiceObservedState.AUTHORIZATION_EXPIRED,
                        workload_id=service.backend_workload_id,
                        workload_uid=service.backend_workload_uid,
                        error_code="service_authorization_denied",
                        error_message=str(exc),
                    )
            return await backend.reconcile(service=service, revision=revision, profile=profile)
        except Exception as exc:
            self.logger.exception(
                "Managed-service reconciliation failed",
                extra={"service_id": service.service_id, "team_id": service.owner.team_id},
            )
            return ServiceBackendObservation(
                observed_state=ServiceObservedState.FAILED,
                error_code="service_reconciliation_failed",
                error_message=str(exc),
            )


__all__ = [
    "ManagedServiceBackend",
    "ManagedServicePublisher",
    "ManagedServiceReconciler",
    "ServiceBackendObservation",
]
