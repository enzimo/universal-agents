from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any

import pytest

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from custom_agents.access_runtime import decide_call
from custom_agents.execution_control_service import ExecutionControlService
from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_tools.runtime_context import set_agent_ref
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.builds import DeterministicBuilder
from runtime_hosting.controller_service import build_execution_access_guard
from runtime_hosting.execution_adapters import (
    ExecutionAdapterError,
    LocalBatchExecutionAdapter,
)
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    AutomationJobV2,
    AutomationScheduleKind,
    AutomationScheduleV1,
    CodeArtifactV1,
    ExecutionActivationKind,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    ResourceLimitsV1,
    RunAttemptStatus,
    RunAttemptV1,
)
from runtime_hosting.profiles import ExecutionProfileRegistry
from runtime_hosting.runner import BatchRunController, ExecutionLeaseRenewalError
from runtime_hosting.scheduling import ScheduleMaterializer
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.execution_control_repository import (
    SqliteExecutionControlRepository,
    StaleExecutionFence,
)
from tests.change_approval_support import CEDAR_BINARY, needs_cedar, use_signed_contexts
from tests.execution_access_support import (
    LOCAL_PROFILE_ID,
    TEAM,
    activation as build_activation,
    execution_instance,
    execution_lead,
    grant_execution,
    grant_unconfined,
    revoke_grant,
)

_AGREEMENT_GUARD = ExecutionAccessGuard(authorization_enabled=False)


def _access_guard(tmp_path: Path) -> ExecutionAccessGuard:
    """Authorization on: the real Cedar evaluator and an empty SQLite access store."""

    catalogs = CatalogSource(None)
    access = AccessService(
        store=AccessRepository(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        ),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id=TEAM,
    )
    return ExecutionAccessGuard(authorization_enabled=True, access=access, catalogs=catalogs)


def _owner() -> OwnerScopeV1:
    return OwnerScopeV1(
        scope_kind=OwnerScopeKind.TEAM,
        owner_id="team_alpha",
        team_id="team_alpha",
    )


def _profile(
    *, allow_host: bool = True, wall_time_seconds: int = 10
) -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id="python.local.test",
        language=ExecutionLanguage.PYTHON,
        backend=ExecutionBackendKind.LOCAL,
        runtime_executable=sys.executable,
        toolchain_identity=f"cpython-{sys.version_info.major}.{sys.version_info.minor}",
        allow_controller_host_execution=allow_host,
        network_enabled=True,
        limits=ResourceLimitsV1(
            wall_time_seconds=wall_time_seconds,
            cpu_time_seconds=10,
            memory_bytes=536_870_912,
            process_count=16,
            writable_bytes=16_777_216,
            artifact_bytes=1_048_576,
        ),
    )


def _revision(
    store: FilesystemArtifactStore,
    profile: ExecutionProfileV1,
    *,
    source: bytes,
) -> tuple[CodeArtifactV1, Any, ExecutionRevisionV1]:
    code = CodeArtifactV1(
        owner=_owner(),
        language=ExecutionLanguage.PYTHON,
        source=store.put_bytes(
            kind=ArtifactKind.SOURCE,
            payload=source,
            media_type="text/x-python",
        ),
        source_name="main.py",
    )
    build = DeterministicBuilder(store, "builder:test").build(
        code=code, profile=profile
    )
    revision = ExecutionRevisionV1.build_revision(
        owner=code.owner,
        language=code.language,
        build=build,
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        entrypoint=build.entrypoint,
    )
    return code, build, revision


def _job(
    revision: ExecutionRevisionV1,
    *,
    mode: ChangeApprovalMode,
) -> AutomationJobV2:
    scheduled = datetime.now(timezone.utc) - timedelta(seconds=1)
    staged = AutomationJobV2(
        job_id="exec_job_runner",
        name="Runner integration",
        owner=revision.owner,
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        schedule=AutomationScheduleV1(
            schedule_kind=AutomationScheduleKind.ONE_SHOT,
            one_shot_at_utc=scheduled,
        ),
        next_run_at_utc=scheduled,
        created_by_task_id="task_execution",
    )
    activation = build_activation(
        ExecutionActivationKind.RUN,
        resource_id=staged.job_id,
        resource_digest=staged.activation_resource_digest(),
        generation=staged.generation,
        owner_scope=staged.owner,
        mode=mode,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return AutomationJobV2.model_validate(
        {**staged.model_dump(mode="python"), "activation": activation, "enabled": True}
    )


@pytest.mark.asyncio
async def test_local_adapter_requires_explicit_operator_opt_in(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    profile = _profile(allow_host=False)
    _, _, revision = _revision(store, profile, source=b"print('no')\n")
    attempt = RunAttemptV1(
        occurrence_id="direct:denied",
        job_id="direct",
        owner=_owner(),
        revision_id=revision.revision_id,
        revision_digest=revision.revision_digest,
        job_generation=1,
        scheduled_for_utc=datetime.now(timezone.utc),
    )

    with pytest.raises(ExecutionAdapterError, match="not enabled"):
        await LocalBatchExecutionAdapter(store, tmp_path / "runs").execute(
            profile=profile,
            revision=revision,
            attempt=attempt,
        )


@needs_cedar
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (ChangeApprovalMode.HELD_ACCESS, "no longer holds write access"),
        (ChangeApprovalMode.AGREEMENT, "Authorization is on now"),
    ],
)
async def test_batch_runner_refuses_without_current_access_and_never_starts_the_process(
    tmp_path: Path, mode: ChangeApprovalMode, reason: str
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    profile = _profile()
    code, build, revision = _revision(store, profile, source=b"print('must-not-run')\n")
    repository.put_code_artifact(code)
    repository.put_build_attestation(build)
    repository.put_revision(revision)
    repository.create_job(_job(revision, mode=mode))
    ScheduleMaterializer(repository).materialize_due_jobs(
        team_id="team_alpha",
        now=datetime.now(timezone.utc),
    )
    guard = _access_guard(tmp_path)
    runner = BatchRunController(
        repository=repository,
        profiles=ExecutionProfileRegistry((profile,)),
        authorization_guard=guard,
        adapters={
            ExecutionBackendKind.LOCAL: LocalBatchExecutionAdapter(
                store, tmp_path / "runs"
            )
        },
        team_id="team_alpha",
        runner_id="runner_test",
    )

    try:
        finished = await runner.run_once()
    finally:
        assert guard.access is not None
        guard.access.cedar.close()

    assert finished is not None
    assert finished.status == RunAttemptStatus.AUTHORIZATION_EXPIRED
    assert finished.error_code == "execution_authorization_denied"
    assert reason in (finished.error_message or "")
    assert finished.backend_workload_id is None


@needs_cedar
@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["execution", "unconfined"])
async def test_a_held_access_job_runs_until_its_grant_is_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoked: str
) -> None:
    """The hook allows the call, the controller's own access check lets it run, revoking stops it.

    The test profile runs code on the controller host, so the job also needs
    full access to code:unconfined, and revoking that stops the next run too.
    """

    use_signed_contexts(monkeypatch)
    instance = execution_instance(tmp_path, authorization_enabled=True)
    agent = execution_lead(instance)
    grants = {"execution": grant_execution(agent), "unconfined": grant_unconfined(agent)}
    set_agent_ref(agent)
    try:
        decision = decide_call(agent, "create_execution_job", {"name": "every minute"})
        assert decision is not None and decision.allowed
        service = ExecutionControlService(agent)
        revision = service.promote_revision(
            source="print('held-access-ok')\n",
            source_name="main.py",
            language="python",
            profile_id=LOCAL_PROFILE_ID,
        )
        job = service.create_job(
            name="every minute",
            revision_id=revision.revision_id,
            schedule=AutomationScheduleV1(schedule_kind=AutomationScheduleKind.INTERVAL, interval_seconds=60),
            activate=True,
        )
    finally:
        set_agent_ref(None)
    guard = build_execution_access_guard(instance.config)
    runner = BatchRunController(
        repository=service.repository,
        profiles=service.profiles,
        authorization_guard=guard,
        adapters={
            ExecutionBackendKind.LOCAL: LocalBatchExecutionAdapter(
                service.artifact_store, tmp_path / "runs"
            )
        },
        team_id=TEAM,
        runner_id="runner_test",
    )
    materializer = ScheduleMaterializer(service.repository)
    now = datetime.now(timezone.utc)
    try:
        materializer.materialize_due_jobs(team_id=TEAM, now=now)
        first = await runner.run_once()
        revoke_grant(agent, grants[revoked])
        materializer.materialize_due_jobs(team_id=TEAM, now=now + timedelta(seconds=61))
        second = await runner.run_once()
    finally:
        assert guard.access is not None
        guard.access.cedar.close()

    assert first is not None and first.status == RunAttemptStatus.SUCCEEDED, first
    assert "held-access-ok" in first.stdout_preview
    assert second is not None and second.job_id == job.job_id
    assert second.status == RunAttemptStatus.AUTHORIZATION_EXPIRED
    expected = "no longer holds write access" if revoked == "execution" else "outside any sandbox"
    assert expected in (second.error_message or "")
    assert second.backend_workload_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    ["parent_cancelled", "renewal_failed", "fence_lost", "renewal_blocked"],
)
async def test_runner_stops_real_workload_when_ownership_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    profile = _profile()
    code, build, revision = _revision(
        store, profile, source=b"import time\ntime.sleep(60)\n"
    )
    repository.put_code_artifact(code)
    repository.put_build_attestation(build)
    repository.put_revision(revision)
    repository.create_job(_job(revision, mode=ChangeApprovalMode.AGREEMENT))
    ScheduleMaterializer(repository).materialize_due_jobs(team_id="team_alpha")
    started = asyncio.Event()
    observed: dict[str, Any] = {}
    adapter = LocalBatchExecutionAdapter(store, tmp_path / "runs")
    renewal_started, release_renewal = threading.Event(), threading.Event()
    adapter_stopped = asyncio.Event()

    class ObservedAdapter:
        async def execute(self, **kwargs: Any) -> Any:
            original_started = kwargs["started_callback"]
            observed["attempt"] = kwargs["attempt"]

            async def capture(pid: str, uid: str | None, digest: str | None) -> None:
                await original_started(pid, uid, digest)
                observed["pid"] = int(pid)
                started.set()

            kwargs["started_callback"] = capture
            try:
                return await adapter.execute(**kwargs)
            finally:
                observed["adapter_finished"] = True
                adapter_stopped.set()

    runner = BatchRunController(
        repository=repository,
        profiles=ExecutionProfileRegistry((profile,)),
        authorization_guard=_AGREEMENT_GUARD,
        adapters={ExecutionBackendKind.LOCAL: ObservedAdapter()},
        team_id="team_alpha",
        runner_id="runner_test",
        lease_ttl_seconds=3,
    )
    error = (
        StaleExecutionFence("replaced owner")
        if stop_reason == "fence_lost"
        else sqlite3.OperationalError("database unavailable")
    )

    def fail_renewal(**_kwargs: Any) -> None:
        raise error

    original_renewal = repository.renew_run_attempt_lease

    def blocked_renewal(**kwargs: Any) -> RunAttemptV1:
        renewal_started.set()
        assert release_renewal.wait(8)
        return original_renewal(**kwargs)

    if stop_reason == "renewal_blocked":
        monkeypatch.setattr(repository, "renew_run_attempt_lease", blocked_renewal)
    elif stop_reason != "parent_cancelled":
        monkeypatch.setattr(repository, "renew_run_attempt_lease", fail_renewal)
    parent = asyncio.create_task(runner.run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if stop_reason == "parent_cancelled":
            parent.cancel()
            expected = asyncio.CancelledError
        else:
            expected = (
                StaleExecutionFence
                if stop_reason == "fence_lost"
                else ExecutionLeaseRenewalError
            )
        if stop_reason == "renewal_blocked":
            assert await asyncio.to_thread(renewal_started.wait, 3)
            # The last confirmed deadline must stop the real process while the
            # storage worker remains blocked. Cleanup still waits for that I/O.
            await asyncio.wait_for(adapter_stopped.wait(), timeout=4)
            with pytest.raises(ProcessLookupError):
                os.kill(observed["pid"], 0)
            assert not parent.done()
            release_renewal.set()
        with pytest.raises(expected):
            await asyncio.wait_for(parent, timeout=5)
        assert observed["adapter_finished"] is True
        with pytest.raises(ProcessLookupError):
            os.kill(observed["pid"], 0)
        persisted = repository.get_run_attempt(
            team_id="team_alpha",
            run_id=observed["attempt"].run_id,
        )
        # Loss of the controller's authority is not proof of a backend outcome.
        # The durable identity and fence remain available for recovery.
        assert persisted is not None
        assert persisted.status == RunAttemptStatus.RUNNING
        assert persisted.backend_workload_id == str(observed["pid"])
        assert persisted.fence == 1
    finally:
        release_renewal.set()
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
