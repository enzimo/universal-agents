from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from runtime_hosting.artifacts import ArtifactIntegrityError, FilesystemArtifactStore
from runtime_hosting.builds import (
    BUILD_LOCK_FILENAME,
    BuildPolicyError,
    DeterministicBuilder,
    OpenShellRustBuilder,
)
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    CodeArtifactV1,
    ExecutionBackendKind,
    ExecutionLanguage,
    ExecutionProfileV1,
    OwnerScopeKind,
    OwnerScopeV1,
)


def _owner() -> OwnerScopeV1:
    return OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id="team_alpha", team_id="team_alpha")


def _profile(
    *,
    language: ExecutionLanguage = ExecutionLanguage.PYTHON,
    backend: ExecutionBackendKind = ExecutionBackendKind.LOCAL,
) -> ExecutionProfileV1:
    return ExecutionProfileV1.build(
        profile_id=f"{language.value}.{backend.value}.restricted",
        language=language,
        backend=backend,
        runtime_executable="python3" if language == ExecutionLanguage.PYTHON else "program",
        builder_executable="/usr/bin/rustc" if language == ExecutionLanguage.RUST else None,
        toolchain_identity="test-toolchain",
        openshell_sandbox_name="rust-builder" if backend == ExecutionBackendKind.OPENSHELL else None,
    )


def _code(
    store: FilesystemArtifactStore,
    *,
    language: ExecutionLanguage,
    source: bytes,
    source_name: str,
) -> CodeArtifactV1:
    reference = store.put_bytes(
        kind=ArtifactKind.SOURCE,
        payload=source,
        media_type="text/plain",
    )
    return CodeArtifactV1(
        owner=_owner(),
        language=language,
        source=reference,
        source_name=source_name,
    )


def test_filesystem_store_deduplicates_by_digest_and_materializes_verified(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    first = store.put_bytes(kind=ArtifactKind.SOURCE, payload=b"print('ok')\n", media_type="text/x-python")
    second = store.put_bytes(kind=ArtifactKind.SOURCE, payload=b"print('ok')\n", media_type="text/x-python")
    materialized = store.materialize_verified(
        first,
        destination_dir=tmp_path / "run",
        filename="main.py",
    )

    assert first.sha256 == second.sha256
    assert first.storage_key == second.storage_key
    assert materialized.read_bytes() == b"print('ok')\n"
    assert not Path(first.storage_key).is_absolute()


def test_filesystem_store_rejects_tampering_and_noncanonical_keys(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(
        kind=ArtifactKind.RESULT,
        payload=b"original",
        media_type="application/octet-stream",
    )
    object_path = store.root / reference.storage_key
    object_path.chmod(0o640)
    object_path.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="digest"):
        store.read_verified(reference.model_copy(update={"size_bytes": len(b"tampered")}))
    with pytest.raises(ArtifactIntegrityError, match="not canonical"):
        store.read_verified(reference.model_copy(update={"storage_key": "../outside"}))


def test_filesystem_store_rejects_symlink_input(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="regular file"):
        store.put_file(kind=ArtifactKind.SOURCE, source=link, media_type="text/x-python")


def test_python_builder_rejects_invalid_source(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    code = _code(
        store,
        language=ExecutionLanguage.PYTHON,
        source=b"def broken(:\n",
        source_name="main.py",
    )

    with pytest.raises(BuildPolicyError, match="did not compile"):
        DeterministicBuilder(store, "builder:test").build(code=code, profile=_profile())


def test_rust_builder_fails_closed_on_controller_host(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    code = _code(
        store,
        language=ExecutionLanguage.RUST,
        source=b"fn main() {}\n",
        source_name="main.rs",
    )

    with pytest.raises(BuildPolicyError, match="cannot run on the controller host"):
        DeterministicBuilder(store, "builder:test").build(
            code=code,
            profile=_profile(language=ExecutionLanguage.RUST),
        )


def test_rust_builder_invokes_direct_isolated_rustc_and_promotes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    store = FilesystemArtifactStore(workspace / "artifacts")
    code = _code(
        store,
        language=ExecutionLanguage.RUST,
        source=b"fn main() { println!(\"ok\"); }\n",
        source_name="main.rs",
    )
    profile = _profile(
        language=ExecutionLanguage.RUST,
        backend=ExecutionBackendKind.OPENSHELL,
    )
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = tuple(argv)
        observed["kwargs"] = kwargs
        output = next((workspace / "runs" / "rust-builds").glob("*/program"), None)
        assert output is None
        build_dir = next((workspace / "runs" / "rust-builds").iterdir())
        (build_dir / "program").write_bytes(b"ELF-test")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    builder = DeterministicBuilder(
        store,
        "builder:test",
        isolated_rust_builder=OpenShellRustBuilder(
            host_workspace_root=workspace,
            build_root=workspace / "runs" / "rust-builds",
            openshell_executable="/usr/local/bin/openshell",
        ),
    )

    attestation = builder.build(code=code, profile=profile)

    assert store.read_verified(attestation.output) == b"ELF-test"
    argv = observed["argv"]
    assert isinstance(argv, tuple)
    assert argv[:3] == ("/usr/local/bin/openshell", "sandbox", "exec")
    assert "/usr/bin/rustc" in argv


def test_a_rust_build_starts_over_when_the_cleanup_deleted_its_folder_while_it_waited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saved output cleanup deletes an old build folder, lock file included, while holding its lock.

    A build that was waiting on that lock then holds a lock on a file no
    longer in the folder; it must start over in a new folder rather than
    build in a deleted one or beside another build.
    """

    import fcntl
    import shutil

    workspace = tmp_path / "workspace"
    build_root = workspace / "runs" / "rust-builds"
    store = FilesystemArtifactStore(workspace / "artifacts")
    code = _code(store, language=ExecutionLanguage.RUST, source=b"fn main() {}\n", source_name="main.rs")
    profile = _profile(language=ExecutionLanguage.RUST, backend=ExecutionBackendKind.OPENSHELL)
    real_flock = fcntl.flock
    locks: list[int] = []

    def lock_after_the_cleanup_deleted_the_folder(descriptor, operation):
        real_flock(descriptor, operation)
        locks.append(operation)
        if len(locks) == 1:
            shutil.rmtree(build_root)  # what the cleanup did while this build waited

    def fake_run(argv, **kwargs):
        build_dir = next(build_root.iterdir())
        assert (build_dir / BUILD_LOCK_FILENAME).is_file() and (build_dir / "main.rs").is_file()
        (build_dir / "program").write_bytes(b"ELF-test")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(fcntl, "flock", lock_after_the_cleanup_deleted_the_folder)
    monkeypatch.setattr(subprocess, "run", fake_run)
    builder = DeterministicBuilder(
        store,
        "builder:test",
        isolated_rust_builder=OpenShellRustBuilder(
            host_workspace_root=workspace, build_root=build_root, openshell_executable="/usr/local/bin/openshell"
        ),
    )

    attestation = builder.build(code=code, profile=profile)

    assert store.read_verified(attestation.output) == b"ELF-test"
    assert len(locks) == 2
