"""Data folders: folders outside the workspace registered as access targets (decision D16).

An administrator registers a host folder under a short name; agents then name
it ``data/<name>`` (``access_control.folders``) and reach it only through
folder grants, and a folder registered as not writable refuses writes whatever
the grants say. The records live in the access store, so every process (the
agents, the gateway, the broker) reads the current list on use and a new
folder needs no restart.

Registering a host path is what exposes it at all, so the rules are checked
twice: when the folder is registered and again every time a process reads the
list. A folder may not hold or sit inside a protected location (the instance
root with its control files and databases, the instance configuration, the
broker's credentials), may not look like a credential store, and may not
overlap another data folder (one path would then have two folder ids, and a
block on one would not cover the other).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from access_control.entities import PersonPrincipal
from access_control.folders import DataFolder, data_folder_problem
from access_control.people import is_administrator
from access_control.sensitive_paths import sensitive_reason
from custom_messages.contracts.access_v2 import (
    DATA_FOLDER_NAME_PATTERN,
    DataFolderRecordV1,
    SubjectKind,
    SubjectRefV1,
)
from id_generation import new_id
from storage.access_repository import AccessRepository, AccessStoreConflict

logger = logging.getLogger(__name__)

_NAME = re.compile(DATA_FOLDER_NAME_PATTERN)


class DataFolderRefused(ValueError):
    """A data folder cannot be registered or removed; the message says why."""


def protected_locations(
    instance_root: str | Path | None,
    *,
    instance_config: str | Path | None = None,
    broker_credentials: str | Path | None = None,
) -> tuple[Path, ...]:
    """The locations no data folder may hold or sit inside, where this process knows them."""

    return tuple(Path(value) for value in (instance_root, instance_config, broker_credentials) if value)


def _overlap(real: Path, others: Iterable[DataFolderRecordV1]) -> str | None:
    for other in others:
        existing = Path(os.path.realpath(other.path))
        if real == existing or existing in real.parents or real in existing.parents:
            return f"it overlaps the data folder {other.name!r} ({other.path})"
    return None


def registration_problem(
    name: str, path: str, *, protected: Iterable[Path], existing: Iterable[DataFolderRecordV1] = ()
) -> str | None:
    """Why a folder cannot be registered under ``name``, or None."""

    if not _NAME.fullmatch(str(name or "")):
        return f"The name {name!r} must be lowercase letters, digits, '-' or '_' (at most 64)."
    candidate = Path(str(path or "").strip()).expanduser()
    if not candidate.is_absolute() or candidate == Path(candidate.anchor):
        return f"{path!r} must be an absolute folder, not the filesystem root."
    real = Path(os.path.realpath(candidate))
    if not real.is_dir():
        return f"{path} is not an existing folder on this host (in Docker, mount it into the container first)."
    problem = data_folder_problem(real, protected)
    if problem:
        return problem
    reason = sensitive_reason(real)
    if reason:
        return f"{path} looks like a credential store ({reason}), which agents must never reach."
    overlap = _overlap(real, [folder for folder in existing if folder.name != name])
    if overlap:
        return f"{path} cannot be registered: {overlap}."
    return None


@dataclass
class DataFolderRegistry:
    """The team's data folders in the access store."""

    store: AccessRepository
    team_id: str

    def records(self) -> list[DataFolderRecordV1]:
        return self.store.data_folders(self.team_id)

    def usable(self, protected: Iterable[Path]) -> tuple[DataFolder, ...]:
        """The registered folders that still pass the rules; a folder that fails is left out and logged."""

        guarded = tuple(protected)
        folders: list[DataFolder] = []
        for record in self.records():
            real = Path(os.path.realpath(record.path))
            problem = data_folder_problem(real, guarded) or (
                f"looks like a credential store ({reason})" if (reason := sensitive_reason(real)) else None
            )
            if problem:
                logger.error(
                    "A data folder was left out because it reaches protected runtime files. "
                    "event=data_folder_refused name=%s path=%s problem=%s",
                    record.name,
                    record.path,
                    problem,
                )
                continue
            folders.append(DataFolder(record.name, real, record.writable))
        return tuple(folders)

    def new_record(
        self, person: PersonPrincipal, *, name: str, path: str, writable: bool, reason: str, now: datetime
    ) -> DataFolderRecordV1:
        """A validated record, not yet stored (proposals store it with their other changes)."""

        return DataFolderRecordV1(
            record_id=new_id("data_folder"),
            team_id=self.team_id,
            name=str(name).strip(),
            path=str(Path(os.path.realpath(Path(str(path).strip()).expanduser()))),
            writable=bool(writable),
            reason=str(reason or "").strip()[:2000],
            registered_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            created_at_utc=now,
        )

    def check(self, person: PersonPrincipal, name: str, path: str, *, protected: Iterable[Path]) -> None:
        """Raise ``DataFolderRefused`` unless the person may register this folder."""

        if not is_administrator(person.roles):
            raise DataFolderRefused("Only an owner or an administrator can register a data folder.")
        problem = registration_problem(name, path, protected=protected, existing=self.records())
        if problem:
            raise DataFolderRefused(problem)

    def register(
        self,
        person: PersonPrincipal,
        *,
        name: str,
        path: str,
        writable: bool,
        reason: str,
        protected: Iterable[Path],
        now: datetime,
    ) -> DataFolderRecordV1:
        guarded = tuple(protected)
        self.check(person, name, path, protected=guarded)
        record = self.new_record(person, name=name, path=path, writable=writable, reason=reason, now=now)
        try:
            stored = self.store.register_data_folder(record)
        except AccessStoreConflict as exc:
            raise DataFolderRefused(str(exc)) from exc
        logger.warning(
            "A data folder was registered. event=data_folder_registered name=%s path=%s writable=%s by=%s",
            stored.name,
            stored.path,
            stored.writable,
            person.person_id,
        )
        return stored

    def remove(self, person: PersonPrincipal, name: str, *, now: datetime) -> DataFolderRecordV1:
        if not is_administrator(person.roles):
            raise DataFolderRefused("Only an owner or an administrator can remove a data folder.")
        try:
            removed = self.store.remove_data_folder(
                self.team_id, str(name).strip(), actor=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id), at=now
            )
        except KeyError as exc:
            raise DataFolderRefused(f"There is no data folder named {name!r}.") from exc
        logger.warning(
            "A data folder was removed. event=data_folder_removed name=%s path=%s by=%s",
            removed.name,
            removed.path,
            person.person_id,
        )
        return removed


__all__ = [
    "DataFolderRefused",
    "DataFolderRegistry",
    "protected_locations",
    "registration_problem",
]
