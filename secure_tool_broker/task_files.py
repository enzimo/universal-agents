"""Local files for broker-run CLIs, limited to the requesting person's folders.

Some Google commands read or write local files (``drive upload``,
``--attach``, ``--out``). The broker runs outside the code sandbox and holds
the credentials, so an agent must never make it open an arbitrary path: that
would let it upload ``team.env`` to Drive or overwrite the instance
configuration with an attachment.

Paths are limited to the folders of the person the request is for (their
folder under ``workspaces/users`` plus the shared ``team/`` and ``agents/``;
see ``runtime_settings.instance_layout``). Files are opened one path component
at a time without following symbolic links (``access_control.safe_open``), and
never handed to the CLI directly: inputs are copied into a private temporary
folder before the CLI runs, and outputs are written by the CLI into that folder
and copied out afterwards. The CLI therefore never opens a path the agent
controls, which also closes check-then-use races with symlinks the agent could
swap.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from access_control import safe_open
from access_control.safe_open import UnsafePath
from runtime_settings.instance_layout import InstanceLayout, WorkspaceScope


MAX_STAGED_FILE_BYTES = 50 * 1024 * 1024
MAX_STAGED_OUTPUT_FILES = 200


class TaskFileRejected(ValueError):
    """A local path is outside the task's folders or cannot be opened safely."""


@dataclass(frozen=True)
class _Located:
    root: Path  # one of the scope roots
    parts: tuple[str, ...]  # components below the root
    display: Path


def _locate(raw: str, scope: WorkspaceScope) -> _Located:
    text = str(raw or "").strip()
    if not text:
        raise TaskFileRejected("A local file path is empty.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = scope.home / candidate
    normalized = Path(os.path.normpath(candidate))
    for root in scope.roots:
        if normalized == root or root in normalized.parents:
            return _Located(root, normalized.relative_to(root).parts, normalized)
    raise TaskFileRejected(
        f"Local files must be in this task's folders ({scope.home} or the shared team/ "
        f"and agents/ folders): {normalized}"
    )


@contextmanager
def _rejected_when_unsafe() -> Iterator[None]:
    try:
        yield
    except UnsafePath as exc:
        raise TaskFileRejected(str(exc)) from exc


def _require_folder(located: _Located, parts: tuple[str, ...]) -> None:
    """The folder ``root/parts`` must exist now and be reached without links."""

    with _rejected_when_unsafe():
        os.close(safe_open.open_directory(located.root, parts))


@dataclass
class TaskFileStage:
    """A private temporary folder for one CLI run."""

    scope: WorkspaceScope
    temp_dir: Path
    _outputs: list[tuple[Path, _Located, bool]] = field(default_factory=list)
    _counter: int = 0

    @classmethod
    def create(cls, *, instance_root: str | None, person_id: str | None) -> "TaskFileStage":
        root = str(instance_root or "").strip()
        if not root:
            raise TaskFileRejected(
                "Local files are not available through the broker here: the instance root is unknown."
            )
        scope = InstanceLayout.at(root).scope_for(person_id)
        temp_dir = Path(tempfile.mkdtemp(prefix="ua-broker-files-"))
        os.chmod(temp_dir, 0o700)
        return cls(scope=scope, temp_dir=temp_dir)

    def _slot(self) -> Path:
        self._counter += 1
        slot = self.temp_dir / f"{self._counter:03d}"
        slot.mkdir(mode=0o700)
        return slot

    def input_file(self, raw: str) -> str:
        """Copy a file from the task's folders; returns the private copy's path."""

        located = _locate(raw, self.scope)
        if not located.parts:
            raise TaskFileRejected(f"A task folder is not a file: {located.display}")
        target = self._slot() / located.parts[-1]
        with _rejected_when_unsafe():
            safe_open.copy_file(located.root, located.parts, target, max_bytes=MAX_STAGED_FILE_BYTES)
        return str(target)

    def output_file(self, raw: str) -> str:
        """A private path for the CLI to write; copied to ``raw`` by ``publish``."""

        located = _locate(raw, self.scope)
        if not located.parts:
            raise TaskFileRejected(f"Give a file name inside the task folder: {located.display}")
        _require_folder(located, located.parts[:-1])
        target = self._slot() / located.parts[-1]
        self._outputs.append((target, located, False))
        return str(target)

    def output_path(self, raw: str) -> str:
        """An output that may name an existing folder or a file inside one."""

        located = _locate(raw, self.scope)
        try:
            _require_folder(located, located.parts)
        except TaskFileRejected:
            return self.output_file(raw)
        return self.output_directory(raw)

    def output_directory(self, raw: str) -> str:
        """A private folder for the CLI to write; its files are copied by ``publish``."""

        located = _locate(raw, self.scope)
        _require_folder(located, located.parts)
        target = self._slot()
        self._outputs.append((target, located, True))
        return str(target)

    def publish(self) -> list[str]:
        """Copy what the CLI wrote into the task's folders; returns the paths written."""

        written: list[str] = []
        for private, located, is_directory in self._outputs:
            if is_directory:
                names = sorted(entry.name for entry in os.scandir(private) if entry.is_file(follow_symlinks=False))
                for name in names[:MAX_STAGED_OUTPUT_FILES]:
                    self._write_into(located, (*located.parts, name), private / name)
                    written.append(str(located.display / name))
            elif private.is_file():
                self._write_into(located, located.parts, private)
                written.append(str(located.display))
        return written

    @staticmethod
    def _write_into(located: _Located, parts: tuple[str, ...], source: Path) -> None:
        """Write a new file, or replace a one-link regular file in one rename; never through a link."""

        with _rejected_when_unsafe():
            with safe_open.writable_file(located.root, parts) as writer, open(source, "rb") as reader:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


__all__ = ["MAX_STAGED_FILE_BYTES", "TaskFileRejected", "TaskFileStage"]
