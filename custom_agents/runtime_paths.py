"""Shared runtime path resolution for agent-owned resources."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from runtime_settings.instance_layout import WORKSPACES_DIRNAME, InstanceLayout


def resolve_runtime_instance_root(
    *,
    instance_root: str | Path | None = None,
    bootstrap_config: Mapping[str, Any] | None = None,
    instance_config_path: str | Path | None = None,
    workspace_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the canonical instance root without trusting where the config file lives.

    A process may be started from a config file outside the instance root
    (``TEAM_INSTANCE_CONFIG``). The instance root stays available in bootstrap
    metadata and through ``TEAM_INSTANCE_ROOT``; only direct launches infer it
    from the config or workspace location.
    """
    explicit_root = str(instance_root or "").strip()
    if explicit_root:
        return str(Path(explicit_root).expanduser().resolve())
    bootstrap_root = (
        str(bootstrap_config.get("instance_root") or "").strip()
        if isinstance(bootstrap_config, Mapping)
        else ""
    )
    if bootstrap_root:
        return str(Path(bootstrap_root).expanduser().resolve())

    env_mapping = env if env is not None else os.environ
    env_root = str(env_mapping.get("TEAM_INSTANCE_ROOT") or "").strip()
    if env_root:
        return str(Path(env_root).expanduser().resolve())

    if instance_config_path:
        return str(Path(instance_config_path).expanduser().resolve().parent)
    if workspace_dir:
        # workspace_dir is <root>/workspaces; a folder with another name is an
        # embedded agent's self-contained root.
        workspace = Path(workspace_dir).expanduser().resolve()
        return str(workspace.parent if workspace.name == WORKSPACES_DIRNAME else workspace)
    return None


INSTANCE_ROOT_NOT_CONFIGURED = "instance_root_not_configured"
logger = logging.getLogger(__name__)


def infer_instance_root(agent: Any) -> str:
    """The instance root of an agent, by one rule used everywhere.

    Order: the agent's configured root, bootstrap metadata, ``TEAM_INSTANCE_ROOT``,
    the instance config file's folder, the parent of a ``workspaces/`` working
    folder, any other working folder itself (an embedded agent keeps its files
    inside that folder), and finally the configured default root ``.``.
    """

    return resolve_runtime_instance_root(
        instance_root=getattr(agent, "instance_root", None),
        bootstrap_config=dict(getattr(agent, "bootstrap_config", {}) or {}),
        instance_config_path=getattr(agent, "instance_config_path", None),
        workspace_dir=getattr(agent, "workspace_dir", None),
    ) or str(Path(".").resolve())


def fill_missing_instance_paths(agent: Any) -> None:
    """Give an agent constructed without an instance root one, and log it.

    Runtimes built from configuration always pass the root. Embedded and test
    starts use infer_instance_root; the working folder defaults to the
    layout's ``workspaces/``.
    """

    if not str(getattr(agent, "instance_root", "") or "").strip():
        agent.instance_root = infer_instance_root(agent)
        logger.warning(
            "Agent started without an instance root; using %s. "
            "event=instance_root_inferred agent_id=%s workspace_dir=%s",
            agent.instance_root,
            getattr(agent, "id", None),
            getattr(agent, "workspace_dir", None),
        )
    if not str(getattr(agent, "workspace_dir", "") or "").strip():
        agent.workspace_dir = str(InstanceLayout.at(agent.instance_root).workspaces_dir)


def agent_layout(agent: Any) -> InstanceLayout:
    """The instance layout of a running agent (see infer_instance_root)."""

    return InstanceLayout.at(infer_instance_root(agent))


def agent_team_composition_path(agent: Any) -> Path:
    """TEAM_AGENTS.yaml for this agent: its resolved path, else the layout's."""

    explicit = str(getattr(agent, "team_composition_path", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return agent_layout(agent).team_composition_path
