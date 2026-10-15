"""Fenced batch runner that performs the final PEP immediately before launch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import secrets

from runtime_hosting.blocking_io import BlockingIOLane

from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
)
from runtime_hosting.execution_adapters import (
    BatchExecutionAdapter,
    BatchExecutionResult,
)
from runtime_hosting.execution_contracts import (
    ExecutionBackendKind,
    RunAttemptStatus,
    RunAttemptV1,
)
from runtime_hosting.profiles import ExecutionProfileNotFound, ExecutionProfileRegistry
from runtime_hosting.kubernetes import KubernetesOutcomeUnknown
from storage.execution_control_repository import (
    ExecutionControlRepository,
    StaleExecutionFence,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionLeaseRenewalError(RuntimeError):
    """The workload owner could not establish continued lease validity."""


@dataclass
class _RunLeaseCoordinator:
    repository: ExecutionControlRepository
    runner_id: str
    lease_token: str
    fence: int
    lease_ttl_seconds: int
    state: RunAttemptV1
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _io: BlockingIOLane = field(default_factory=BlockingIOLane)
    _renewed: asyncio.Event = field(default_factory=asyncio.Event)

    def _accept_renewal(self, state: RunAttemptV1) -> None:
        self.state = state
        self._renewed.set()

    async def watch_expiration(self) -> None:
        """Stop work at the last confirmed deadline even if storage is stuck."""
        while True:
            self._renewed.clear()
            expires = self.state.lease_expires_at_utc
            remaining = (expires - _now()).total_seconds() if expires else 0.0
            if remaining <= 0:
                raise ExecutionLeaseRenewalError(
                    f"Execution lease expired for run {self.state.run_id}."
                )
            try:
                await asyncio.wait_for(self._renewed.wait(), timeout=remaining)
            except TimeoutError:
                # Recheck the confirmed record: a renewal may have arrived in
                # the same event-loop tick as the timer.
                continue

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.lease_ttl_seconds / 3))
            try:
                async with self.state_lock:
                    await self._io.run(
                        self.repository.renew_run_attempt_lease,
                        committed=self._accept_renewal,
                        team_id=self.state.owner.team_id,
                        run_id=self.state.run_id,
                        runner_id=self.runner_id,
                        lease_token=self.lease_token,
                        fence=self.fence,
                        lease_ttl_seconds=self.lease_ttl_seconds,
                    )
            except StaleExecutionFence:
                raise
            except Exception as exc:
                raise ExecutionLeaseRenewalError(
                    f"Could not renew execution lease for run {self.state.run_id}."
                ) from exc

    async def started(
        self,
        workload_id: str,
        workload_uid: str | None,
        spec_digest: str | None,
    ) -> None:
        async with self.state_lock:
            candidate = self.state.model_copy(
                update={
                    "status": RunAttemptStatus.RUNNING,
                    "backend_workload_id": workload_id,
                    "backend_workload_uid": workload_uid,
                    "backend_spec_digest": spec_digest,
                    "started_at_utc": _now(),
                    "updated_at_utc": _now(),
                }
            )
            await self._io.run(
                self.repository.update_claimed_run_attempt,
                candidate,
                committed=lambda _: setattr(self, "state", candidate),
                runner_id=self.runner_id,
                lease_token=self.lease_token,
                fence=self.fence,
            )


@dataclass
class BatchRunController:
    repository: ExecutionControlRepository
    profiles: ExecutionProfileRegistry
    authorization_guard: ExecutionAccessGuard
    adapters: dict[ExecutionBackendKind, BatchExecutionAdapter]
    team_id: str
    runner_id: str
    lease_ttl_seconds: int = 60
    logger: logging.Logger = logging.getLogger(__name__)
    _io: BlockingIOLane = field(default_factory=BlockingIOLane, init=False)

    async def run_once(self) -> RunAttemptV1 | None:
        lease_token = secrets.token_urlsafe(24)
        attempt = await self._io.run(
            self.repository.claim_next_run_attempt,
            team_id=self.team_id,
            runner_id=self.runner_id,
            lease_token=lease_token,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        if attempt is None:
            return None
        return await self._run_claimed(attempt, lease_token=lease_token)

    async def _run_claimed(
        self, attempt: RunAttemptV1, *, lease_token: str
    ) -> RunAttemptV1:
        fence = attempt.fence
        job = await self._io.run(
            self.repository.get_job,
            team_id=attempt.owner.team_id,
            job_id=attempt.job_id,
        )
        revision = await self._io.run(
            self.repository.get_revision,
            team_id=attempt.owner.team_id,
            revision_id=attempt.revision_id,
        )
        if job is None or revision is None:
            return await self._finish_error(
                attempt,
                lease_token=lease_token,
                fence=fence,
                status=RunAttemptStatus.OUTCOME_UNKNOWN,
                error_code="trusted_record_missing",
                message="The exact job or revision disappeared before execution.",
            )
        try:
            profile = await self._io.run(
                self.profiles.require,
                profile_id=revision.profile_id,
                profile_digest=revision.profile_digest,
            )
            adapter = self.adapters[profile.backend]
        except (ExecutionProfileNotFound, KeyError) as exc:
            return await self._finish_error(
                attempt,
                lease_token=lease_token,
                fence=fence,
                status=RunAttemptStatus.FAILED,
                error_code="execution_profile_unavailable",
                message=str(exc),
            )
        try:
            await self._io.run(
                self.authorization_guard.require_run,
                job=job,
                attempt=attempt,
                revision=revision,
                # Only the local backend runs code on the controller host itself.
                host_execution=profile.backend is ExecutionBackendKind.LOCAL,
            )
        except ExecutionAuthorizationDenied as exc:
            return await self._finish_error(
                attempt,
                lease_token=lease_token,
                fence=fence,
                status=RunAttemptStatus.AUTHORIZATION_EXPIRED,
                error_code="execution_authorization_denied",
                message=str(exc),
            )

        state = attempt.model_copy(
            update={
                "status": RunAttemptStatus.STARTING,
                "backend_kind": profile.backend,
                "updated_at_utc": _now(),
            }
        )
        await self._io.run(
            self.repository.update_claimed_run_attempt,
            state,
            runner_id=self.runner_id,
            lease_token=lease_token,
            fence=fence,
        )
        coordinator = _RunLeaseCoordinator(
            repository=self.repository,
            runner_id=self.runner_id,
            lease_token=lease_token,
            fence=fence,
            lease_ttl_seconds=self.lease_ttl_seconds,
            state=state,
        )
        heartbeat_task = asyncio.create_task(coordinator.heartbeat())
        expiration_task = asyncio.create_task(coordinator.watch_expiration())
        execution_task = asyncio.create_task(
            adapter.execute(
                profile=profile,
                revision=revision,
                attempt=coordinator.state,
                started_callback=coordinator.started,
            )
        )
        try:
            done, _ = await asyncio.wait(
                {execution_task, heartbeat_task, expiration_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Renewal failure wins even when execution finishes in the same tick.
            # A successful adapter result cannot establish authority to commit it.
            if expiration_task in done:
                expiration_task.result()
            if heartbeat_task in done:
                heartbeat_task.result()
                raise ExecutionLeaseRenewalError(
                    "Execution heartbeat stopped unexpectedly."
                )
            result = await execution_task
        except StaleExecutionFence:
            raise
        except ExecutionLeaseRenewalError:
            self.logger.exception(
                "Execution lease renewal failed; stopping the owned workload",
                extra={
                    "event": "execution_lease_renewal_failed",
                    "run_id": attempt.run_id,
                    "job_id": attempt.job_id,
                    "runner_id": self.runner_id,
                    "fence": fence,
                },
            )
            # Leave the durable attempt for fenced recovery. A failed renewal
            # must not be followed by an unfenced or speculative terminal write.
            raise
        except KubernetesOutcomeUnknown as exc:
            return await self._finish_error(
                coordinator.state,
                lease_token=lease_token,
                fence=fence,
                status=RunAttemptStatus.OUTCOME_UNKNOWN,
                error_code="kubernetes_outcome_unknown",
                message=str(exc),
            )
        except Exception as exc:
            self.logger.exception(
                "Execution adapter failed",
                extra={"run_id": attempt.run_id, "job_id": attempt.job_id},
            )
            return await self._finish_error(
                coordinator.state,
                lease_token=lease_token,
                fence=fence,
                status=RunAttemptStatus.FAILED,
                error_code="execution_adapter_failed",
                message=str(exc),
            )
        finally:
            # Parent cancellation must reach the adapter, whose cleanup owns the
            # local process group or the backend's explicit adoption semantics.
            for child in (execution_task, heartbeat_task, expiration_task):
                if not child.done():
                    child.cancel()
            await asyncio.gather(
                execution_task, heartbeat_task, expiration_task, return_exceptions=True
            )

        return await self._finish_result(
            coordinator.state,
            result=result,
            lease_token=lease_token,
            fence=fence,
        )

    async def _finish_result(
        self,
        state: RunAttemptV1,
        *,
        result: BatchExecutionResult,
        lease_token: str,
        fence: int,
    ) -> RunAttemptV1:
        status = self._result_status(result)
        finished = state.model_copy(
            update={
                "status": status,
                "finished_at_utc": _now(),
                "exit_code": result.exit_code,
                "stdout_preview": result.stdout_preview,
                "stderr_preview": result.stderr_preview,
                "log_artifact": result.log_artifact,
                "backend_workload_id": result.backend_workload_id
                or state.backend_workload_id,
                "backend_workload_uid": result.backend_workload_uid
                or state.backend_workload_uid,
                "error_code": None
                if status == RunAttemptStatus.SUCCEEDED
                else status.value,
                "updated_at_utc": _now(),
            }
        )
        return await self._io.run(
            self.repository.finish_run_attempt,
            finished,
            runner_id=self.runner_id,
            lease_token=lease_token,
            fence=fence,
        )

    @staticmethod
    def _result_status(result: BatchExecutionResult) -> RunAttemptStatus:
        if result.timed_out:
            return RunAttemptStatus.TIMED_OUT
        if result.cancelled:
            return RunAttemptStatus.CANCELLED
        if result.exit_code == 0:
            return RunAttemptStatus.SUCCEEDED
        return RunAttemptStatus.FAILED

    async def _finish_error(
        self,
        attempt: RunAttemptV1,
        *,
        lease_token: str,
        fence: int,
        status: RunAttemptStatus,
        error_code: str,
        message: str,
    ) -> RunAttemptV1:
        finished = attempt.model_copy(
            update={
                "status": status,
                "finished_at_utc": _now(),
                "error_code": error_code,
                "error_message": message,
                "updated_at_utc": _now(),
            }
        )
        return await self._io.run(
            self.repository.finish_run_attempt,
            finished,
            runner_id=self.runner_id,
            lease_token=lease_token,
            fence=fence,
        )


__all__ = ["BatchRunController", "ExecutionLeaseRenewalError"]
