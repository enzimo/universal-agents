"""Streaming batch execution adapters for exact immutable revisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import resource
import signal
import shutil
import tempfile
from typing import Protocol

from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    ExecutionRevisionV1,
    RunAttemptV1,
)


OutputCallback = Callable[[str, bytes], Awaitable[None]]
StartedCallback = Callable[[str, str | None, str | None], Awaitable[None]]


class ExecutionAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchExecutionResult:
    exit_code: int | None
    stdout_preview: str
    stderr_preview: str
    log_artifact: ArtifactReferenceV1
    timed_out: bool = False
    cancelled: bool = False
    backend_workload_id: str | None = None
    backend_workload_uid: str | None = None


class BatchExecutionAdapter(Protocol):
    async def execute(
        self,
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        attempt: RunAttemptV1,
        output_callback: OutputCallback | None = None,
        started_callback: StartedCallback | None = None,
    ) -> BatchExecutionResult: ...


@dataclass
class _CapturedOutput:
    stdout_limit: int
    stderr_limit: int
    log_limit: int
    stdout: bytearray
    stderr: bytearray
    log: bytearray

    @classmethod
    def for_profile(cls, profile: ExecutionProfileV1) -> "_CapturedOutput":
        return cls(
            stdout_limit=profile.limits.stdout_bytes,
            stderr_limit=profile.limits.stderr_bytes,
            log_limit=profile.limits.artifact_bytes,
            stdout=bytearray(),
            stderr=bytearray(),
            log=bytearray(),
        )

    def append(self, channel: str, payload: bytes) -> None:
        preview = self.stdout if channel == "stdout" else self.stderr
        preview_limit = self.stdout_limit if channel == "stdout" else self.stderr_limit
        if len(preview) < preview_limit:
            preview.extend(payload[: preview_limit - len(preview)])
        if len(self.log) < self.log_limit:
            record = json.dumps(
                {"channel": channel, "text": payload.decode("utf-8", errors="replace")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            self.log.extend(record[: self.log_limit - len(self.log)])


@dataclass(frozen=True)
class LocalBatchExecutionAdapter:
    artifact_store: ArtifactStore
    run_root: Path
    logger: logging.Logger = logging.getLogger(__name__)

    async def execute(
        self,
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        attempt: RunAttemptV1,
        output_callback: OutputCallback | None = None,
        started_callback: StartedCallback | None = None,
    ) -> BatchExecutionResult:
        if profile.backend != ExecutionBackendKind.LOCAL:
            raise ExecutionAdapterError("Local adapter received a non-local profile.")
        if not profile.allow_controller_host_execution:
            raise ExecutionAdapterError("Controller-host execution is not enabled by this profile.")
        if not profile.network_enabled:
            raise ExecutionAdapterError(
                "The local adapter cannot enforce network isolation; use OpenShell or Kubernetes."
            )
        self.run_root.expanduser().resolve().mkdir(parents=True, exist_ok=True)
        work_dir = Path(
            tempfile.mkdtemp(prefix=f"{attempt.run_id}.", dir=self.run_root.expanduser().resolve())
        )
        try:
            entrypoint = self.artifact_store.materialize_verified(
                revision.build.output,
                destination_dir=work_dir,
                filename=revision.entrypoint,
                executable=revision.language == ExecutionLanguage.RUST,
            )
            argv = self._argv(profile=profile, revision=revision, entrypoint=entrypoint)
            return await self._execute_process(
                argv=argv,
                cwd=work_dir,
                env=self._environment(revision),
                profile=profile,
                output_callback=output_callback,
                started_callback=started_callback,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _execute_process(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        profile: ExecutionProfileV1,
        output_callback: OutputCallback | None,
        started_callback: StartedCallback | None,
        enforce_resource_limits: bool = True,
    ) -> BatchExecutionResult:
        captured = _CapturedOutput.for_profile(profile)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=(
                self._resource_limiter(profile) if enforce_resource_limits else None
            ),
        )

        if started_callback is not None:
            try:
                await started_callback(str(process.pid), None, None)
            except Exception:
                await self._terminate_group(process)
                raise

        drains = (
            asyncio.create_task(
                self._drain("stdout", process.stdout, captured, output_callback)
            ),
            asyncio.create_task(
                self._drain("stderr", process.stderr, captured, output_callback)
            ),
        )
        timed_out = False
        cancelled = False
        try:
            await asyncio.wait_for(process.wait(), timeout=profile.limits.wall_time_seconds)
        except TimeoutError:
            timed_out = True
            await self._terminate_group(process)
        except asyncio.CancelledError:
            cancelled = True
            await self._terminate_group(process)
            raise
        finally:
            await asyncio.gather(*drains, return_exceptions=True)
        log_reference = self.artifact_store.put_bytes(
            kind=ArtifactKind.LOG,
            payload=bytes(captured.log),
            media_type="application/x-ndjson",
        )
        return BatchExecutionResult(
            exit_code=process.returncode,
            stdout_preview=captured.stdout.decode("utf-8", errors="replace"),
            stderr_preview=captured.stderr.decode("utf-8", errors="replace"),
            log_artifact=log_reference,
            timed_out=timed_out,
            cancelled=cancelled,
            backend_workload_id=str(process.pid),
        )

    @staticmethod
    async def _terminate_group(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    @staticmethod
    async def _drain(
        channel: str,
        stream: asyncio.StreamReader | None,
        captured: _CapturedOutput,
        output_callback: OutputCallback | None,
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(16_384):
            captured.append(channel, chunk)
            if output_callback is not None:
                await output_callback(channel, chunk)

    @staticmethod
    def _argv(
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        entrypoint: Path,
    ) -> tuple[str, ...]:
        if revision.language == ExecutionLanguage.PYTHON:
            return (
                profile.runtime_executable,
                "-I",
                "-B",
                str(entrypoint),
                *revision.args,
            )
        return (str(entrypoint), *revision.args)

    @staticmethod
    def _environment(revision: ExecutionRevisionV1) -> dict[str, str]:
        fixed = {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        fixed.update(revision.non_secret_env)
        return fixed

    @staticmethod
    def _resource_limiter(profile: ExecutionProfileV1) -> Callable[[], None]:
        limits = profile.limits

        def apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_time_seconds, limits.cpu_time_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (limits.process_count, limits.process_count))
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.writable_bytes, limits.writable_bytes))

        return apply


@dataclass(frozen=True)
class OpenShellBatchExecutionAdapter(LocalBatchExecutionAdapter):
    openshell_executable: str = "openshell"
    host_workspace_root: Path = Path(".")

    async def execute(
        self,
        *,
        profile: ExecutionProfileV1,
        revision: ExecutionRevisionV1,
        attempt: RunAttemptV1,
        output_callback: OutputCallback | None = None,
        started_callback: StartedCallback | None = None,
    ) -> BatchExecutionResult:
        if profile.backend != ExecutionBackendKind.OPENSHELL or not profile.openshell_sandbox_name:
            raise ExecutionAdapterError("OpenShell adapter requires an exact OpenShell profile.")
        workspace = self.host_workspace_root.expanduser().resolve()
        run_root = self.run_root.expanduser().resolve()
        if not run_root.is_relative_to(workspace):
            raise ExecutionAdapterError("OpenShell run_root must be inside its shared workspace.")
        run_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f"{attempt.run_id}.", dir=run_root))
        try:
            host_entrypoint = self.artifact_store.materialize_verified(
                revision.build.output,
                destination_dir=work_dir,
                filename=revision.entrypoint,
                executable=revision.language == ExecutionLanguage.RUST,
            )
            relative_work_dir = work_dir.relative_to(workspace).as_posix()
            sandbox_work_dir = f"{profile.openshell_workspace_mount.rstrip('/')}/{relative_work_dir}"
            sandbox_entrypoint = f"{sandbox_work_dir}/{host_entrypoint.name}"
            runtime_argv = self._argv(
                profile=profile,
                revision=revision,
                entrypoint=Path(sandbox_entrypoint),
            )
            env_argv = tuple(
                f"{key}={value}" for key, value in sorted(self._environment(revision).items())
            )
            argv = (
                self.openshell_executable,
                "sandbox",
                "exec",
                "--no-tty",
                "--timeout",
                str(profile.limits.wall_time_seconds),
                "--workdir",
                sandbox_work_dir,
                "-n",
                profile.openshell_sandbox_name,
                "--",
                "/usr/bin/env",
                *env_argv,
                *runtime_argv,
            )
            async def report_started(
                _process_id: str,
                _uid: str | None,
                _spec_digest: str | None,
            ) -> None:
                if started_callback is not None:
                    await started_callback(
                        f"{profile.openshell_sandbox_name}:{attempt.run_id}",
                        None,
                        None,
                    )

            result = await self._execute_process(
                argv=argv,
                cwd=workspace,
                env={
                    "HOME": os.environ.get("HOME", "/nonexistent"),
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                },
                profile=profile,
                output_callback=output_callback,
                started_callback=report_started,
                enforce_resource_limits=False,
            )
            return BatchExecutionResult(
                **{
                    **result.__dict__,
                    "backend_workload_id": f"{profile.openshell_sandbox_name}:{attempt.run_id}",
                }
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


__all__ = [
    "BatchExecutionAdapter",
    "BatchExecutionResult",
    "ExecutionAdapterError",
    "LocalBatchExecutionAdapter",
    "OpenShellBatchExecutionAdapter",
    "OutputCallback",
    "StartedCallback",
]
