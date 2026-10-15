"""Which filesystem paths agent tools may open.

Agent tools work inside ``workspaces/`` (runtime_settings.instance_layout):
the current task's person folder plus the shared ``team/`` and ``agents/``
folders (custom_tools.workspace_scope), and in the data folders an
administrator registers (``access_control.data_folders``). Everything else is
refused: under the instance root that means control files, databases, runtime
records, credentials and operator files; outside it, every path that is not in
a data folder. Another person's folder passes here only with authorization on,
where the access service decides it from what its owner shared (decision
D15). With authorization on, the access service then decides each folder
(``custom_tools.file_access``), so a data folder is reached only with a grant.

The team's skill folders are read-only: agents may read them (a skill's
reference files and scripts) but never write them
(``InstanceLayout.agent_readable_skill_dirs``), and a data folder registered
as not writable is read-only the same way.

Relative paths resolve against the task's default folder (the person's folder,
or ``agents/`` for work that has no person).

These rules judge where a path really points (links resolved). They never
open anything: tools open files through ``custom_tools.file_access``, which
applies these rules and then opens the checked location without following
links, so nothing can be swapped in between the check and the open.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any


from access_control.folders import DataFolder
from access_control.sensitive_paths import sensitive_reason
from custom_tools.workspace_scope import (
    configured_data_folders,
    current_instance_layout,
    current_workspace_scope,
)
from runtime_settings.instance_layout import (
    USERS_DIRNAME,
    InstanceLayout,
    WorkspaceScope,
)


logger = logging.getLogger(__name__)

WORKSPACE_PATH_DENIED = "workspace_path_denied"
PERSON_WORKSPACE_DENIED = "person_workspace_denied"
PROTECTED_RUNTIME_CONTROL_DENIED = "protected_runtime_control_denied"
SENSITIVE_PATH_DENIED = "sensitive_path_denied"
TEAM_SKILL_READ_ONLY = "team_skill_read_only"
DATA_FOLDER_READ_ONLY = "data_folder_read_only"


class PathPolicyError(PermissionError):
    """A local filesystem-policy denial that is independent of Cedar authority."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code




def _resolve_in_scope(path: str | Path, scope: WorkspaceScope) -> Path:
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = scope.home / candidate
    return candidate.resolve()


def _configured_control_paths() -> set[Path]:
    """The operator's instance configuration file, which may sit outside the root.

    Every other control location (MCP connections, hosted MCP servers) is
    derived from the instance root (``InstanceLayout``) and is closed by
    ``InstanceLayout.is_instance_private``.
    """

    instance_config = str(os.getenv("TEAM_INSTANCE_CONFIG") or "").strip()
    if not instance_config:
        return set()
    return {Path(instance_config).expanduser().resolve()}


def _is_same_or_child(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def readable_skill_dir(resolved: Path, agent: Any = None) -> Path | None:
    """The read-only team skill folder containing ``resolved``, if any."""

    layout = current_instance_layout(agent)
    return layout.agent_readable_skill_dir_of(resolved) if layout is not None else None


def protected_runtime_control_kind(path: str | Path, agent: Any = None) -> str | None:
    """Why a path is closed to agent tools regardless of workspace settings.

    Team skill folders are not reported: reading them is allowed, and
    ``ensure_allowed_path`` refuses writes to them separately.
    """

    scope = current_workspace_scope(agent)
    resolved = _resolve_in_scope(path, scope)
    layout = current_instance_layout(agent)
    if layout is not None and layout.agent_readable_skill_dir_of(resolved) is not None:
        return None
    if layout is not None and layout.is_instance_private(resolved):
        return _instance_private_kind(layout, resolved)
    for configured in _configured_control_paths():
        if _is_same_or_child(resolved, configured) or configured in resolved.parents:
            return "runtime_control_file"
    return None


def _instance_private_kind(layout: InstanceLayout, resolved: Path) -> str:
    if _is_same_or_child(resolved, layout.control_dir):
        return "control_file"
    if _is_same_or_child(resolved, layout.dbs_dir):
        return "managed_runtime_database"
    if _is_same_or_child(resolved, layout.state_dir):
        return "runtime_record"
    return "instance_private"


def is_protected_runtime_control_path(path: str | Path) -> bool:
    return protected_runtime_control_kind(path) is not None


def _private_area_literals() -> list[str]:
    layout = current_instance_layout()
    literals = [str(path).lower() for path in _configured_control_paths()]
    if layout is not None:
        literals += [
            str(path).lower()
            for path in (
                layout.control_dir,
                layout.dbs_dir,
                layout.state_dir,
                layout.root / "runtime",
                layout.root / "auth",
                layout.root / "instance_config.yaml",
            )
        ]
    return literals


def _without_skill_dir_references(normalized: str) -> str:
    """Remove references into the readable skill folders (``..`` excluded)."""

    layout = current_instance_layout()
    if layout is None:
        return normalized
    for skill_dir in layout.agent_readable_skill_dirs:
        pattern = re.escape(str(skill_dir).lower()) + r"(?!/\.\.)"
        normalized = re.sub(pattern, " ", normalized)
    return normalized


def contains_protected_runtime_control_reference(value: str) -> bool:
    """Conservatively detect literal references to areas closed to agents.

    This is defense in depth for command text; the sandbox and the path checks
    above are the boundary. Paths into the readable skill folders are allowed
    so commands can run a skill's bundled scripts.
    """

    normalized = _without_skill_dir_references(
        str(value or "").replace("\\", "/").lower()
    )
    return any(literal and literal in normalized for literal in _private_area_literals())


def _protected_message(kind: str, resolved: Path) -> str:
    if kind == "managed_runtime_database":
        return (
            f"The runtime databases cannot be opened through agent tools: {resolved}. "
            "Use the task, conversation, memory, and access tools, which apply "
            "per-person access checks."
        )
    if kind == "control_file":
        return (
            f"Team control files cannot be opened through generic tools: {resolved}. "
            "Read them through get_runtime_configuration_context, get_team_composition "
            "and get_available_agent_types; change them through plan_runtime_setting_change, "
            "the team composition tools, and reviewed agent or skill installation."
        )
    return (
        f"This runtime area is closed to agent tools: {resolved}. Agents work in "
        "their person's folder and the shared team/ and agents/ folders."
    )


def in_other_persons_folder(resolved: Path, scope: WorkspaceScope) -> bool:
    """Whether a real path is inside a person's folder other than the task person's own."""

    users_dir = scope.workspaces_dir / USERS_DIRNAME
    if not _is_same_or_child(resolved, users_dir) or resolved == users_dir:
        return False
    return not (scope.person_id is not None and _is_same_or_child(resolved, scope.home))


def _shares_decided(agent: Any) -> bool:
    """With authorization on, the access service decides other people's folders (their shares)."""

    from custom_agents.access_runtime import authorization_enabled

    try:
        return agent is not None and authorization_enabled(getattr(agent, "authorization_config", None))
    except ValueError:
        return False


def _check_other_person(resolved: Path, scope: WorkspaceScope, agent: Any) -> bool:
    """Refuse another person's folder unless shares can apply; returns whether it is one.

    A person's folder is private. With authorization on its owner may share
    part of it with the person this task works for (decision D15); the access
    service then decides each use, so the path passes here. The users folder
    itself is never a target.
    """

    users_dir = scope.workspaces_dir / USERS_DIRNAME
    if resolved == users_dir:
        raise PathPolicyError(
            f"The folder that holds everyone's personal folders is not open to agents: {resolved}.",
            reason_code=PERSON_WORKSPACE_DENIED,
        )
    if not in_other_persons_folder(resolved, scope):
        return False
    if _shares_decided(agent):
        return True
    raise PathPolicyError(
        f"Another person's folder is private: {resolved}. Use this person's folder "
        f"({scope.home}) or the shared team/ and agents/ folders.",
        reason_code=PERSON_WORKSPACE_DENIED,
    )


def _check_sensitive(resolved: Path) -> None:
    reason = sensitive_reason(resolved)
    if reason:
        raise PathPolicyError(f"Sensitive {reason} is blocked: {resolved}", reason_code=SENSITIVE_PATH_DENIED)


def _check_readable_skill_path(resolved: Path, *, for_write: bool) -> Path:
    if for_write:
        raise PathPolicyError(
            f"Team skills are read-only: {resolved}. Write a draft skill folder in "
            "this task's folders and ask TeamArchitect to propose it with "
            "plan_team_skill_change; the person reviews every skill change.",
            reason_code=TEAM_SKILL_READ_ONLY,
        )
    _check_sensitive(resolved)
    return resolved


def data_folder_of(resolved: Path, agent: Any = None) -> DataFolder | None:
    """The registered data folder holding the real path ``resolved``, if any."""

    for folder in configured_data_folders(agent):
        root = Path(os.path.realpath(folder.path))
        if _is_same_or_child(resolved, root):
            return folder
    return None


def ensure_allowed_path(path: str, *, for_write: bool = False, agent: Any = None) -> Path:
    """Resolve ``path`` for the agent's current task, or raise ``PathPolicyError``.

    Reads and writes use the task's folders. The team skill folders are
    readable (``for_write=False``) and never writable. This judges where the
    path really points; tools then open it through ``custom_tools.file_access``,
    never by name.
    """

    scope = current_workspace_scope(agent)
    resolved = _resolve_in_scope(path, scope)
    if readable_skill_dir(resolved, agent) is not None:
        return _check_readable_skill_path(resolved, for_write=for_write)
    kind = protected_runtime_control_kind(resolved, agent)
    if kind:
        raise PathPolicyError(
            _protected_message(kind, resolved),
            reason_code=PROTECTED_RUNTIME_CONTROL_DENIED,
        )
    shared_candidate = _check_other_person(resolved, scope, agent)
    if not shared_candidate and not scope.contains(resolved):
        data = data_folder_of(resolved, agent)
        if data is None:
            raise PathPolicyError(
                f"Path is outside this task's folders: {resolved}. Use {scope.home}, the shared "
                "team/ and agents/ folders, or a data folder the operator registered.",
                reason_code=WORKSPACE_PATH_DENIED,
            )
        if for_write and not data.writable:
            raise PathPolicyError(
                f"The data folder {data.name} is read-only: {resolved}.",
                reason_code=DATA_FOLDER_READ_ONLY,
            )
    _check_sensitive(resolved)
    return resolved
