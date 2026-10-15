"""The gateway's copy of people in the access store, for identity tests (real SQLite)."""

from __future__ import annotations

from pathlib import Path

from assistant_gateway.application.ports import GatewayClock
from assistant_gateway.people_directory_sync import PeopleDirectorySync
from assistant_gateway.ports.repositories import IdentityRepository
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings


def access_store_at(tmp_path: Path) -> AccessRepository:
    return AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )


def people_directory(
    tmp_path: Path, identity: IdentityRepository, *, team_id: str, clock: GatewayClock
) -> tuple[PeopleDirectorySync, AccessRepository]:
    """A sync writing to a real access store under ``tmp_path``, and that store."""

    store = access_store_at(tmp_path)
    return PeopleDirectorySync(identity=identity, open_store=lambda: store, team_id=team_id, clock=clock), store
