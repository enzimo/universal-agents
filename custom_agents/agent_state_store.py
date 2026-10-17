"""Runtime-facing wrapper around the storage-backed agent-state repository."""

import logging
from typing import Any, Dict, List, Optional

from default_values import STORAGE_DEFAULTS
from storage.backends import STORAGE_BACKEND_SQLITE
from custom_messages.contracts.control_v1 import AgentStatus
from storage.agent_state_repository import create_agent_state_repository
from storage.config import StorageSettings, resolve_storage_settings


class AgentStateStore:
    """Maintains durable AgentStatus rows via the configured storage repository."""

    def __init__(
        self,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger: logging.Logger = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self._db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._logger = logger or logging.getLogger(__name__)
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository = create_agent_state_repository(
            self._storage_settings,
            logger=self._logger,
        )

    def upsert(self, status: AgentStatus) -> None:
        self._repository.upsert(status)

    def remove(self, agent_id: str) -> bool:
        return self._repository.remove(agent_id)

    def remove_unseen(self, agent_id: str, last_seen_at: Optional[str]) -> bool:
        return self._repository.remove_unseen(agent_id, last_seen_at)

    def get_by_id(self, agent_id: str) -> Optional[AgentStatus]:
        return self._repository.get_by_id(agent_id)

    def get_all(self) -> List[AgentStatus]:
        return self._repository.get_all()

    def query(self, filters: Dict[str, Any]) -> List[AgentStatus]:
        return self._repository.query(filters)
