"""The folders an agent tool may use for the task it is running right now.

One process runs TeamLead and in-process workers for different people, so the
folders cannot come from a process-wide setting. Each tool call finds its agent
through the tool context, the task that agent is executing, and the person that
task works for, taken from the task's signed context
(``custom_agents.access_runtime.verified_person_id``), never from message
fields. The result is a
``WorkspaceScope``: the person's private folder plus the shared ``team/`` and
``agents/`` folders, or only the shared folders for work that has no person.

File tools, ``notify_user`` attachments, research artifacts, Python
environments and the code sandbox all use this one resolver. The registered
data folders (decision D16, ``access_control.data_folders``) come from here
too, read from the access store on use; agents reach them only with folder
grants (``access_control.folders``).
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from access_control.data_folders import DataFolderRegistry, protected_locations
from access_control.folders import DataFolder
from custom_tools.runtime_context import get_agent_ref
from storage.access_repository import AccessRepository
from storage.config import StorageSettings
from runtime_settings.instance_layout import (
    InstanceLayout,
    WorkspaceScope,
    workspace_scope,
)

logger = logging.getLogger(__name__)


def _env_path(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def task_person_id(agent: Any) -> str | None:
    """The person the agent's current task works for, from its signed context only.

    Without a valid signed context (or for system work) there is no person
    and only the shared folders are in reach.
    """

    from custom_agents.access_runtime import verified_person_id

    return verified_person_id(agent)


def workspaces_root(agent: Any = None) -> Path:
    """``<instance_root>/workspaces`` for this runtime."""

    agent = agent if agent is not None else get_agent_ref()
    raw = str(getattr(agent, "workspace_dir", "") or "").strip() or _env_path(
        "AGENT_WORKSPACE_DIR", "WORKSPACE_DIR"
    )
    return Path(raw or os.getcwd()).expanduser().resolve()


def current_instance_layout(agent: Any = None) -> InstanceLayout | None:
    """The instance layout the current agent works inside, if any.

    Returned only when the agent's working area really is that layout's
    ``workspaces/``. An embedded or test agent whose working folder is
    something else runs outside an instance layout: only the task-folder check
    applies to it, because its root would otherwise contain its own work area.
    """

    agent = agent if agent is not None else get_agent_ref()
    raw = str(getattr(agent, "instance_root", "") or "").strip() or _env_path(
        "TEAM_INSTANCE_ROOT"
    )
    if not raw:
        return None
    layout = InstanceLayout.at(raw)
    return layout if layout.workspaces_dir == workspaces_root(agent) else None


def current_workspace_scope(agent: Any = None) -> WorkspaceScope:
    """Folders for the current tool call's task."""

    agent = agent if agent is not None else get_agent_ref()
    person = task_person_id(agent) if agent is not None else None
    return workspace_scope(workspaces_root(agent), person)


@lru_cache(maxsize=8)
def _access_store(storage: StorageSettings) -> AccessRepository:
    return AccessRepository(storage)


# (storage, team, protected locations) -> (access store version, folders)
_data_folder_cache: dict[tuple[Any, ...], tuple[int, tuple[DataFolder, ...]]] = {}
_data_folder_lock = threading.Lock()


def _protected(agent: Any) -> tuple[Path, ...]:
    layout = current_instance_layout(agent)
    root = layout.root if layout is not None else (
        str(getattr(agent, "instance_root", "") or "").strip() or _env_path("TEAM_INSTANCE_ROOT") or None
    )
    return protected_locations(
        root,
        instance_config=_env_path("TEAM_INSTANCE_CONFIG") or None,
        broker_credentials=_env_path("SECURE_TOOL_BROKER_CREDENTIALS_DIR") or None,
    )


def configured_data_folders(agent: Any = None) -> tuple[DataFolder, ...]:
    """The team's registered data folders, read from the access store (decision D16).

    Registration refuses a folder that holds or sits inside the instance, its
    configuration or the broker's credentials; the same rule is applied again
    here, where the process knows those locations, and a folder that breaks
    it is left out and logged, never used. The list is kept until the access
    store's version changes, so a newly registered folder is seen on the next
    use without a restart.
    """

    agent = agent if agent is not None else get_agent_ref()
    storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
    team_id = str(getattr(agent, "team_id", "") or "")
    if storage is None or not team_id:
        return ()
    store = _access_store(storage)
    protected = _protected(agent)
    key = (storage, team_id, protected)
    version = store.version(team_id)
    with _data_folder_lock:
        cached = _data_folder_cache.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
    folders = DataFolderRegistry(store, team_id).usable(protected)
    with _data_folder_lock:
        _data_folder_cache[key] = (version, folders)
    return folders


def ensure_scope_home(scope: WorkspaceScope) -> Path:
    """Create the task's default folder (a person's folder is made on first use)."""

    scope.home.mkdir(parents=True, exist_ok=True)
    for root in scope.roots:
        root.mkdir(parents=True, exist_ok=True)
    return scope.home


def runtime_layout(agent: Any = None) -> InstanceLayout:
    """The layout that holds this agent's runtime records (never ``None``).

    Unlike ``current_instance_layout`` this always answers, using the shared
    inference rule (custom_agents.runtime_paths.infer_instance_root), because a
    tool that writes offloaded results or published sites needs a location
    even for an embedded agent.
    """

    from custom_agents.runtime_paths import infer_instance_root

    agent = agent if agent is not None else get_agent_ref()
    if agent is None:
        return InstanceLayout.at(_env_path("TEAM_INSTANCE_ROOT") or os.getcwd())
    return InstanceLayout.at(infer_instance_root(agent))
