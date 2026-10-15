"""Registering data folders in the access store for tests (decision D16).

Production code registers folders through ``access_control.data_folders``
(with its checks). Tests that need a folder the checks would refuse at
registration, to prove it is refused again when used, store the record
directly here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from custom_messages.contracts.access_v2 import DataFolderRecordV1, SubjectKind, SubjectRefV1
from id_generation import new_id
from storage.access_repository import AccessRepository
from storage.config import StorageSettings, resolve_storage_settings

DATA_FOLDER_TEAM = "team-data"


def sqlite_storage(path: Path) -> StorageSettings:
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(path)}})


def store_data_folder(
    storage: StorageSettings, name: str, path: Path, *, writable: bool = True, team_id: str = DATA_FOLDER_TEAM
) -> DataFolderRecordV1:
    """Store a data folder record as it is, without the registration checks."""

    return AccessRepository(storage).register_data_folder(
        DataFolderRecordV1(
            record_id=new_id("data_folder"),
            team_id=team_id,
            name=name,
            path=str(path),
            writable=writable,
            registered_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
            created_at_utc=datetime.now(timezone.utc),
        )
    )


def agent_with_data_folders(
    storage: StorageSettings, layout: Any, *folders: dict[str, Any], team_id: str = DATA_FOLDER_TEAM, **fields: Any
) -> SimpleNamespace:
    """An agent in single-person mode whose team registered these data folders."""

    for folder in folders:
        store_data_folder(
            storage, folder["name"], Path(folder["path"]), writable=folder.get("writable", True), team_id=team_id
        )
    return SimpleNamespace(
        authorization_config={"enabled": False},
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        team_id=team_id,
        task_manager=SimpleNamespace(storage_settings=storage),
        **fields,
    )
