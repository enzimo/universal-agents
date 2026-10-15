"""Deterministic promotion of quarantined source into runnable revisions."""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from typing import Protocol

from runtime_hosting.artifacts import ArtifactStore
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    BuildAttestationV1,
    CodeArtifactV1,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
)


class BuildPolicyError(RuntimeError):
    """A source cannot be built under the selected operator profile."""


class IsolatedRustBuilder(Protocol):
    def build(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> tuple[bytes, str, str | None]:
        """Return executable bytes, builder identity, and lockfile digest."""


class IsolatedPythonCompiler(Protocol):
    def compile(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> str:
        """Validate source in a child process and return its compiler identity."""


@dataclass(frozen=True)
class SubprocessPythonCompiler:
    """Run Python's parser outside the controller under the profile limits."""

    executable: str = sys.executable

    def compile(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> str:
        compiler = (
            "import sys; "
            "compile(sys.stdin.buffer.read(), sys.argv[1], 'exec', "
            "dont_inherit=True, optimize=2)"
        )
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    (self.executable, "-I", "-B", "-c", compiler, source_name),
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    start_new_session=True,
                    env={"PATH": os.defpath},
                )
            except OSError as exc:
                raise BuildPolicyError(
                    f"Isolated Python compilation could not start: {exc}"
                ) from exc
            try:
                self._apply_limits(process.pid, profile)
                process.communicate(
                    input=source,
                    timeout=profile.limits.wall_time_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise BuildPolicyError("Isolated Python compilation timed out.") from exc
            except Exception:
                process.kill()
                process.communicate()
                raise
            if process.returncode != 0:
                stderr.seek(0)
                detail = stderr.read(4000).decode("utf-8", errors="replace").strip()
                raise BuildPolicyError(
                    f"Python source did not compile in the isolated child: "
                    f"{detail or f'process exited {process.returncode}'}"
                )
        return f"subprocess:{sys.implementation.cache_tag or 'python'}"

    @staticmethod
    def _apply_limits(pid: int, profile: ExecutionProfileV1) -> None:
        limits = profile.limits
        try:
            resource.prlimit(
                pid,
                resource.RLIMIT_CPU,
                (limits.cpu_time_seconds, limits.cpu_time_seconds),
            )
            resource.prlimit(
                pid,
                resource.RLIMIT_AS,
                (limits.memory_bytes, limits.memory_bytes),
            )
            resource.prlimit(
                pid,
                resource.RLIMIT_NPROC,
                (limits.process_count, limits.process_count),
            )
            output_limit = max(4096, limits.stdout_bytes, limits.stderr_bytes)
            resource.prlimit(pid, resource.RLIMIT_FSIZE, (output_limit, output_limit))
        except (OSError, ValueError) as exc:
            raise BuildPolicyError(
                f"Could not install isolated Python compiler limits: {exc}"
            ) from exc


@dataclass(frozen=True)
class OpenShellRustBuilder:
    """Compile one Rust source file inside an operator-selected OpenShell sandbox."""

    host_workspace_root: Path
    build_root: Path
    openshell_executable: str = "openshell"

    def build(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> tuple[bytes, str, str | None]:
        if profile.backend != ExecutionBackendKind.OPENSHELL:
            raise BuildPolicyError("The OpenShell Rust builder requires an OpenShell profile.")
        if Path(source_name).name != source_name or not source_name.endswith(".rs"):
            raise BuildPolicyError("Rust source_name must be a single .rs filename.")
        if not profile.openshell_sandbox_name or not profile.builder_executable:
            raise BuildPolicyError(
                "The Rust profile must pin an OpenShell sandbox and builder executable."
            )
        workspace = self.host_workspace_root.expanduser().resolve()
        root = self.build_root.expanduser().resolve()
        if not root.is_relative_to(workspace):
            raise BuildPolicyError("The Rust build root must be inside the shared workspace.")
        source_digest = hashlib.sha256(source).hexdigest()
        build_dir = root / source_digest
        build_dir.mkdir(parents=True, exist_ok=True)
        if build_dir.is_symlink():
            raise BuildPolicyError("The content-addressed Rust build directory is a symlink.")
        lock_path = build_dir / ".build.lock"
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(lock_descriptor, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return self._build_locked(
                source=source,
                profile=profile,
                workspace=workspace,
                build_dir=build_dir,
            )

    def _build_locked(
        self,
        *,
        source: bytes,
        profile: ExecutionProfileV1,
        workspace: Path,
        build_dir: Path,
    ) -> tuple[bytes, str, str | None]:
        sandbox_name = profile.openshell_sandbox_name
        builder_executable = profile.builder_executable
        if not sandbox_name or not builder_executable:
            raise BuildPolicyError(
                "The Rust profile must pin an OpenShell sandbox and builder executable."
            )
        source_path = build_dir / "main.rs"
        output_path = build_dir / "program"
        self._write_source(source_path, source)
        if output_path.is_symlink():
            raise BuildPolicyError("The isolated Rust output path is a symlink.")
        output_path.unlink(missing_ok=True)
        relative_dir = build_dir.relative_to(workspace).as_posix()
        sandbox_dir = f"{profile.openshell_workspace_mount.rstrip('/')}/{relative_dir}"
        sandbox_source = f"{sandbox_dir}/main.rs"
        sandbox_output = f"{sandbox_dir}/program"
        try:
            completed = subprocess.run(
                (
                    self.openshell_executable,
                    "sandbox",
                    "exec",
                    "--no-tty",
                    "--timeout",
                    str(profile.limits.wall_time_seconds),
                    "--workdir",
                    sandbox_dir,
                    "-n",
                    sandbox_name,
                    "--",
                    builder_executable,
                    "--edition=2021",
                    "-C",
                    "opt-level=2",
                    "-C",
                    "debuginfo=0",
                    "-C",
                    "strip=symbols",
                    "--remap-path-prefix",
                    f"{sandbox_dir}=.",
                    sandbox_source,
                    "-o",
                    sandbox_output,
                ),
                cwd=workspace,
                env={
                    "HOME": os.environ.get("HOME", "/nonexistent"),
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=profile.limits.wall_time_seconds + 10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildPolicyError(f"Isolated Rust compilation could not complete: {exc}") from exc
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace")[-4000:]
            raise BuildPolicyError(f"Isolated Rust compilation failed: {error}")
        try:
            output = output_path.read_bytes()
        except OSError as exc:
            raise BuildPolicyError("The isolated Rust builder produced no readable output.") from exc
        return output, f"openshell:{sandbox_name}:{profile.toolchain_identity}", None

    @staticmethod
    def _write_source(path: Path, source: bytes) -> None:
        if path.exists():
            if path.is_symlink() or path.read_bytes() != source:
                raise BuildPolicyError("The content-addressed Rust build directory is inconsistent.")
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class DeterministicBuilder:
    artifact_store: ArtifactStore
    builder_identity: str
    isolated_rust_builder: IsolatedRustBuilder | None = None
    isolated_python_compiler: IsolatedPythonCompiler = field(
        default_factory=SubprocessPythonCompiler
    )

    def build(
        self,
        *,
        code: CodeArtifactV1,
        profile: ExecutionProfileV1,
    ) -> BuildAttestationV1:
        if code.language != profile.language:
            raise BuildPolicyError("Source language does not match the execution profile.")
        source = self.artifact_store.read_verified(code.source)
        if code.language == ExecutionLanguage.PYTHON:
            output, media_type, entrypoint, lock_digest, builder_identity = self._build_python(
                source=source,
                source_name=code.source_name,
                profile=profile,
            )
        else:
            output, media_type, entrypoint, lock_digest, builder_identity = self._build_rust(
                source=source,
                source_name=code.source_name,
                profile=profile,
            )
        output_reference = self.artifact_store.put_bytes(
            kind=ArtifactKind.BUILD,
            payload=output,
            media_type=media_type,
        )
        return BuildAttestationV1.build(
            owner=code.owner,
            source_artifact_id=code.code_artifact_id,
            source_sha256=code.source.sha256,
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            toolchain_identity=profile.toolchain_identity,
            dependency_lock_sha256=lock_digest,
            builder_identity=builder_identity,
            output=output_reference,
            entrypoint=entrypoint,
        )

    def _build_python(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> tuple[bytes, str, str, None, str]:
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildPolicyError("Python source must be valid UTF-8.") from exc
        del text
        compiler_identity = self.isolated_python_compiler.compile(
            source=source,
            source_name=source_name,
            profile=profile,
        )
        return (
            source,
            "application/vnd.ua.python-source",
            source_name,
            None,
            f"{self.builder_identity}:{compiler_identity}",
        )

    def _build_rust(
        self,
        *,
        source: bytes,
        source_name: str,
        profile: ExecutionProfileV1,
    ) -> tuple[bytes, str, str, str | None, str]:
        if profile.backend == ExecutionBackendKind.LOCAL:
            raise BuildPolicyError(
                "Rust builds cannot run on the controller host; select an isolated builder profile."
            )
        if self.isolated_rust_builder is None:
            raise BuildPolicyError("No isolated Rust builder is configured for this runtime.")
        output, builder_identity, lock_digest = self.isolated_rust_builder.build(
            source=source,
            source_name=source_name,
            profile=profile,
        )
        if not output:
            raise BuildPolicyError("The isolated Rust builder returned an empty executable.")
        return output, "application/vnd.ua.rust-executable", Path(source_name).stem, lock_digest, builder_identity


__all__ = [
    "BuildPolicyError",
    "DeterministicBuilder",
    "IsolatedPythonCompiler",
    "IsolatedRustBuilder",
    "OpenShellRustBuilder",
    "SubprocessPythonCompiler",
]
