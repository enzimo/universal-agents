"""Content-addressed artifact storage for untrusted execution inputs and outputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Protocol

from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
)


class ArtifactIntegrityError(RuntimeError):
    """An immutable artifact no longer matches its trusted reference."""


class ArtifactStore(Protocol):
    def put_bytes(
        self,
        *,
        kind: ArtifactKind,
        payload: bytes,
        media_type: str,
    ) -> ArtifactReferenceV1: ...

    def read_verified(self, reference: ArtifactReferenceV1) -> bytes: ...

    def materialize_verified(
        self,
        reference: ArtifactReferenceV1,
        *,
        destination_dir: Path,
        filename: str,
        executable: bool = False,
    ) -> Path: ...


@dataclass(frozen=True)
class FilesystemArtifactStore:
    """Atomic filesystem store whose references contain no absolute host paths."""

    root: Path
    maximum_artifact_bytes: int = 268_435_456

    def __post_init__(self) -> None:
        resolved = self.root.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "root", resolved)
        if self.maximum_artifact_bytes < 1:
            raise ValueError("maximum_artifact_bytes must be positive")

    def put_bytes(
        self,
        *,
        kind: ArtifactKind,
        payload: bytes,
        media_type: str,
    ) -> ArtifactReferenceV1:
        content = bytes(payload)
        self._validate_size(len(content))
        digest = hashlib.sha256(content).hexdigest()
        storage_key = self._storage_key(kind=kind, digest=digest)
        destination = self._path_for_key(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._verify_existing(destination, digest=digest, expected_size=len(content))
        else:
            self._write_atomic(destination, content)
        return ArtifactReferenceV1(
            kind=kind,
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            storage_backend=ArtifactStorageBackend.FILESYSTEM,
            storage_key=storage_key,
        )

    def put_file(
        self,
        *,
        kind: ArtifactKind,
        source: Path,
        media_type: str,
    ) -> ArtifactReferenceV1:
        path = source.expanduser()
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("Artifact source must be a regular file and cannot be a symlink.")
        self._validate_size(file_stat.st_size)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(self.maximum_artifact_bytes + 1)
        finally:
            os.close(descriptor)
        return self.put_bytes(kind=kind, payload=payload, media_type=media_type)

    def read_verified(self, reference: ArtifactReferenceV1) -> bytes:
        path = self._path_for_reference(reference)
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise ArtifactIntegrityError("Artifact object is not a regular file.")
        self._validate_size(file_stat.st_size)
        if file_stat.st_size != reference.size_bytes:
            raise ArtifactIntegrityError("Artifact size does not match its immutable reference.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(self.maximum_artifact_bytes + 1)
        finally:
            os.close(descriptor)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != reference.sha256:
            raise ArtifactIntegrityError("Artifact content digest does not match its reference.")
        return payload

    def materialize_verified(
        self,
        reference: ArtifactReferenceV1,
        *,
        destination_dir: Path,
        filename: str,
        executable: bool = False,
    ) -> Path:
        safe_name = self._safe_filename(filename)
        directory = destination_dir.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / safe_name
        if destination.parent != directory:
            raise ValueError("Artifact destination escaped its materialization directory.")
        payload = self.read_verified(reference)
        self._write_atomic(destination, payload, mode=0o500 if executable else 0o400)
        return destination

    def _path_for_reference(self, reference: ArtifactReferenceV1) -> Path:
        if reference.storage_backend != ArtifactStorageBackend.FILESYSTEM:
            raise ValueError("FilesystemArtifactStore cannot read a non-filesystem reference.")
        expected = self._storage_key(kind=reference.kind, digest=reference.sha256)
        if reference.storage_key != expected:
            raise ArtifactIntegrityError("Artifact storage key is not canonical for its digest.")
        return self._path_for_key(reference.storage_key)

    def _path_for_key(self, storage_key: str) -> Path:
        key = PurePosixPath(storage_key)
        if key.is_absolute() or ".." in key.parts or not key.parts:
            raise ValueError("Artifact storage key must be a canonical relative key.")
        candidate = self.root.joinpath(*key.parts)
        if candidate.resolve(strict=False).is_relative_to(self.root):
            return candidate
        raise ValueError("Artifact storage key escaped the configured store root.")

    @staticmethod
    def _storage_key(*, kind: ArtifactKind, digest: str) -> str:
        return f"{kind.value}/{digest[:2]}/{digest}"

    @staticmethod
    def _safe_filename(filename: str) -> str:
        normalized = str(filename or "").strip()
        if not normalized or Path(normalized).name != normalized or normalized in {".", ".."}:
            raise ValueError("Artifact filename must be one non-empty basename.")
        return normalized

    def _validate_size(self, size_bytes: int) -> None:
        if size_bytes > self.maximum_artifact_bytes:
            raise ValueError(
                f"Artifact contains {size_bytes} bytes; limit is {self.maximum_artifact_bytes}."
            )

    @staticmethod
    def _write_atomic(destination: Path, payload: bytes, *, mode: int = 0o440) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            temporary.replace(destination)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_existing(path: Path, *, digest: str, expected_size: int) -> None:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
            raise ArtifactIntegrityError("Existing content-addressed artifact has invalid metadata.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ArtifactIntegrityError("Existing content-addressed artifact has invalid content.")


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactStore",
    "FilesystemArtifactStore",
]
