"""Folders as access targets (access plan, Phase 5).

A folder is named by an id below a registered root, never by a host path, so
grants stay valid when an instance moves:

- ``team`` and ``agents``: the shared folders under ``workspaces/``; every
  role holds ``write`` on them as a standard grant;
- ``data/<name>``: folders an administrator registers outside the workspace
  (``access_control.data_folders``, kept in the access store); they need
  grants, and one registered as not writable refuses writes whatever the
  grants say;
- ``users/<folder>``: one person's private folder, owned by that person. An
  agent uses the folder of the person its task works for through a standard
  rule. Only the owner may give access to it, and only to other people: a
  share (decision D15). An agent working for the person it was shared with
  may then use it at the shared level. Administrators cannot open it.

Everything else (control files, runtime records, databases, another
instance's paths) is outside every root, so no grant can name it. A grant on a
folder covers everything below it: the Cedar entity of a file is inside the
entity of each folder up to its root (``access_control.entities``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    TEAM_DIRNAME,
    USERS_DIRNAME,
    InstanceLayout,
    person_folder_name,
)

FOLDER_ROOT_TEAM = TEAM_DIRNAME
FOLDER_ROOT_AGENTS = AGENTS_DIRNAME
FOLDER_ROOT_USERS = USERS_DIRNAME
FOLDER_ROOT_DATA = "data"
# Folders every role holds write access to without a grant row.
STANDARD_SHARED_FOLDERS = (FOLDER_ROOT_TEAM, FOLDER_ROOT_AGENTS)
# Stands in for the owner of a person folder that is not the task person's, so
# the owner policy never matches it (no User has this id).
_OTHER_PERSON_OWNER_PREFIX = "folder-owner:"


class FolderIdError(ValueError):
    """A folder id is malformed or names no registered root."""


def _components(raw: str) -> list[str]:
    text = str(raw or "").strip().strip("/")
    if not text or "\\" in text or "\x00" in text:
        raise FolderIdError(f"Not a folder id: {raw!r}. Use team/…, agents/… or data/<name>/….")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FolderIdError(f"A folder id cannot contain empty, '.' or '..' parts: {raw!r}")
    return parts


def root_length(parts: list[str] | tuple[str, ...]) -> int:
    """How many leading parts name the root (``team`` is one; ``data/x`` is two)."""

    if parts and parts[0] in {FOLDER_ROOT_TEAM, FOLDER_ROOT_AGENTS}:
        return 1
    if len(parts) >= 2 and parts[0] in {FOLDER_ROOT_DATA, FOLDER_ROOT_USERS}:
        return 2
    raise FolderIdError(
        f"{'/'.join(parts)!r} is not below a folder root; use team/…, agents/… or data/<name>/…."
    )


def normalize_folder_id(raw: str) -> str:
    """The canonical id of a folder (or file) below a registered root."""

    parts = _components(raw)
    root_length(parts)
    return "/".join(parts)


def grantable_folder_id(raw: str) -> str:
    """A folder (or file) id that grants, requests and blocks may name.

    A person's folder is accepted here; the access service decides who may
    give access to it (its owner only, ``std:owner``).
    """

    return normalize_folder_id(raw)


def is_person_folder(folder_id: str) -> bool:
    """Whether the id names a person's folder or something inside it."""

    return normalize_folder_id(folder_id).split("/", 1)[0] == FOLDER_ROOT_USERS


def person_folder_id(person_id: str) -> str:
    """The folder id of one person's own folder (``users/<folder>``)."""

    return f"{FOLDER_ROOT_USERS}/{person_folder_name(person_id)}"


def folder_chain(target_id: str) -> list[str]:
    """``target_id`` and each folder above it, up to its root, nearest first."""

    parts = normalize_folder_id(target_id).split("/")
    stop = root_length(parts)
    return ["/".join(parts[:end]) for end in range(len(parts), stop - 1, -1)]


def covers(parent_id: str, child_id: str) -> bool:
    """Whether the folder ``parent_id`` is ``child_id`` or contains it."""

    parent = normalize_folder_id(parent_id)
    child = normalize_folder_id(child_id)
    return child == parent or child.startswith(parent + "/")


@dataclass(frozen=True)
class DataFolder:
    """An operator-registered folder outside the workspace."""

    name: str
    path: Path
    writable: bool = True


@dataclass(frozen=True)
class FolderRoots:
    """The registered roots of one instance, with their real paths."""

    workspaces_dir: Path
    data_folders: tuple[DataFolder, ...] = field(default=())

    @classmethod
    def for_layout(cls, layout: InstanceLayout, data_folders: tuple[DataFolder, ...] = ()) -> "FolderRoots":
        return cls(Path(os.path.realpath(layout.workspaces_dir)), data_folders)

    def _roots(self) -> list[tuple[str, Path]]:
        roots = [
            (FOLDER_ROOT_TEAM, self.workspaces_dir / TEAM_DIRNAME),
            (FOLDER_ROOT_AGENTS, self.workspaces_dir / AGENTS_DIRNAME),
        ]
        roots += [(f"{FOLDER_ROOT_DATA}/{folder.name}", Path(os.path.realpath(folder.path))) for folder in self.data_folders]
        return roots

    def data_folder(self, folder_id: str) -> DataFolder | None:
        parts = normalize_folder_id(folder_id).split("/")
        if parts[0] != FOLDER_ROOT_DATA:
            return None
        return next((folder for folder in self.data_folders if folder.name == parts[1]), None)

    def folder_id_of(self, real_path: Path) -> str | None:
        """The folder id of a real (link-free) path, or None when it is under no root."""

        users = self.workspaces_dir / USERS_DIRNAME
        if users in real_path.parents:
            relative = real_path.relative_to(users).parts
            return "/".join((FOLDER_ROOT_USERS, *relative))
        for root_id, root in self._roots():
            if real_path == root or root in real_path.parents:
                return "/".join((root_id, *real_path.relative_to(root).parts))
        return None

    def path_of(self, folder_id: str) -> Path:
        """The host path of a folder id (the id must name a registered root)."""

        parts = normalize_folder_id(folder_id).split("/")
        stop = root_length(parts)
        root_id = "/".join(parts[:stop])
        if parts[0] == FOLDER_ROOT_USERS:
            base = self.workspaces_dir / USERS_DIRNAME / parts[1]
        else:
            base = dict(self._roots()).get(root_id)
            if base is None:
                raise FolderIdError(f"{root_id} is not a registered folder.")
        return base.joinpath(*parts[stop:])


def data_folder_problem(path: Path, protected: Iterable[Path]) -> str | None:
    """Why a data folder cannot be registered, or None.

    A data folder may neither hold nor sit inside a protected location (the
    instance root, the broker's credentials, the instance configuration), or
    folder grants and code mounts would reach control files and secrets.
    """

    real = Path(os.path.realpath(path))
    for location in protected:
        guarded = Path(os.path.realpath(location))
        if real == guarded or guarded in real.parents:
            return f"{path} is inside {location}, which agents must never reach."
        if real in guarded.parents:
            return f"{path} contains {location}, which agents must never reach."
    return None


def folder_owner(folder_id: str, task_person_id: str | None) -> str | None:
    """The owner of a folder for a decision: the task person for their own folder.

    Another person's folder gets an owner no principal has, so only the owner
    policy could ever allow it, and it never does. Team-owned roots have none.
    """

    parts = normalize_folder_id(folder_id).split("/")
    if parts[0] != FOLDER_ROOT_USERS:
        return None
    if task_person_id and parts[1] == person_folder_name(task_person_id):
        return task_person_id
    return _OTHER_PERSON_OWNER_PREFIX + parts[1]


__all__ = [
    "DataFolder",
    "FOLDER_ROOT_AGENTS",
    "FOLDER_ROOT_DATA",
    "FOLDER_ROOT_TEAM",
    "FOLDER_ROOT_USERS",
    "FolderIdError",
    "FolderRoots",
    "STANDARD_SHARED_FOLDERS",
    "covers",
    "data_folder_problem",
    "folder_chain",
    "folder_owner",
    "grantable_folder_id",
    "is_person_folder",
    "normalize_folder_id",
    "person_folder_id",
    "root_length",
]
