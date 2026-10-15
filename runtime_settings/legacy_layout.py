"""Detect files left in the pre-2026-09 single-workspace layout.

Earlier versions kept control files, the task database, runtime records, and
every agent file in one ``<root>/workspace`` folder. The runtime does not move
files itself (project rule: no automatic migration). Startup instead refuses to
run while any old-layout file remains and prints the exact commands that move
each one to its place in ``runtime_settings.instance_layout``.

Content whose owner cannot be known (research files, uploads, anything else the
agents wrote) moves to ``workspaces/team/``: everyone on the team could already
see it, so this keeps its visibility. People can move private items into their
own folders afterwards.
"""

from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runtime_settings.instance_layout import (
    CONVERSATION_ARTIFACTS_DIRNAME,
    AGENT_BUNDLES_DIRNAME,
    AGENT_DEFINITIONS_DIRNAME,
    LEGACY_WORKSPACE_DIRNAME,
    LLM_OVERRIDES_FILENAME,
    OBJECTIVE_PROFILES_DIRNAME,
    SKILLS_DIRNAME,
    SOUL_FILENAME,
    TASK_DATABASE_FILENAME,
    TEAM_COMPOSITION_FILENAME,
    TEAM_TEMPLATES_FILENAME,
    InstanceLayout,
)

LEGACY_LAYOUT_PRESENT = "legacy_instance_layout_present"

_CONTROL_ENTRIES = (
    SOUL_FILENAME,
    LLM_OVERRIDES_FILENAME,
    TEAM_COMPOSITION_FILENAME,
    TEAM_TEMPLATES_FILENAME,
    AGENT_DEFINITIONS_DIRNAME,
    AGENT_BUNDLES_DIRNAME,
    SKILLS_DIRNAME,
    OBJECTIVE_PROFILES_DIRNAME,
)
_DATABASE_ENTRIES = (
    TASK_DATABASE_FILENAME,
    f"{TASK_DATABASE_FILENAME}-wal",
    f"{TASK_DATABASE_FILENAME}-shm",
    CONVERSATION_ARTIFACTS_DIRNAME,
)


@dataclass(frozen=True)
class LayoutMove:
    source: Path
    destination: Path
    # A destination that already exists needs the operator to merge by hand.
    conflict: bool = False


def _state_moves(workspace: Path, layout: InstanceLayout) -> list[tuple[Path, Path]]:
    artifacts = workspace / "artifacts"
    return [
        (artifacts / "tool_results", layout.tool_results_dir),
        (artifacts / "single_page_sites", layout.published_sites_dir),
        (artifacts / "model_failover", layout.model_failover_dir),
        (artifacts / "context_dumps", layout.diagnostics_dir / "context_dumps"),
        (
            artifacts / "context_lifecycle.jsonl",
            layout.diagnostics_dir / "context_lifecycle.jsonl",
        ),
        (artifacts / "web_chat_attachments", layout.uploads_dir),
        (workspace / "runtime" / "execution-artifacts", layout.execution_artifacts_dir),
        (workspace / "runtime" / "execution-runs", layout.execution_runs_dir),
    ]


def _candidate_moves(workspace: Path, layout: InstanceLayout) -> list[tuple[Path, Path]]:
    pairs = [(workspace / name, layout.control_dir / name) for name in _CONTROL_ENTRIES]
    pairs += [(workspace / "dbs" / name, layout.dbs_dir / name) for name in _DATABASE_ENTRIES]
    pairs += _state_moves(workspace, layout)
    return pairs


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def _remaining_workspace_entries(workspace: Path, claimed: set[Path]) -> list[Path]:
    """Old workspace entries not covered by a specific move, for ``team/``."""

    remaining: list[Path] = []
    for entry in sorted(workspace.iterdir()):
        if entry in claimed or _is_empty_dir(entry):
            continue
        if entry.is_dir() and any(entry in path.parents for path in claimed):
            # A folder such as artifacts/ or dbs/ is emptied by specific moves
            # first; whatever is left in it moves as its own entries.
            remaining.extend(
                child
                for child in sorted(entry.iterdir())
                if child not in claimed
                and not _is_empty_dir(child)
                and not any(child in path.parents for path in claimed)
            )
            continue
        remaining.append(entry)
    return remaining


def legacy_layout_moves(root: str | Path) -> list[LayoutMove]:
    """Moves needed before this instance can start; empty when none remain."""

    layout = InstanceLayout.at(root)
    workspace = layout.root / LEGACY_WORKSPACE_DIRNAME
    moves: list[LayoutMove] = []
    claimed: set[Path] = set()
    # A config.yaml development root used to be the workspace itself, so its
    # control files and database sat directly in the root.
    for base in (workspace, layout.root):
        if not base.is_dir():
            continue
        for source, destination in _candidate_moves(base, layout):
            if source == destination or not (source.exists() or source.is_symlink()):
                continue
            claimed.add(source)
            moves.append(LayoutMove(source, destination, destination.exists()))
    if workspace.is_dir():
        for entry in _remaining_workspace_entries(workspace, claimed):
            destination = layout.team_dir / entry.relative_to(workspace)
            moves.append(LayoutMove(entry, destination, destination.exists()))
    return moves


def render_move_commands(root: str | Path, moves: list[LayoutMove]) -> str:
    """Shell commands an operator runs, with every team runtime stopped."""

    layout = InstanceLayout.at(root)
    lines = [
        "# Stop every team runtime first. Then run:",
        "set -e",
        "mkdir -p "
        + " ".join(
            shlex.quote(str(path))
            for path in (
                layout.control_dir,
                layout.state_dir,
                layout.dbs_dir,
                layout.team_dir,
                layout.agents_dir,
                layout.users_dir,
            )
        ),
    ]
    for move in moves:
        if move.conflict:
            lines.append(
                f"# {move.destination} already exists; merge {move.source} into it by hand."
            )
            continue
        parent = move.destination.parent
        lines.append(f"mkdir -p {shlex.quote(str(parent))}")
        lines.append(
            f"mv -n {shlex.quote(str(move.source))} {shlex.quote(str(move.destination))}"
        )
    workspace = layout.root / LEGACY_WORKSPACE_DIRNAME
    quoted = shlex.quote(str(workspace))
    lines.append(
        f"if [ -d {quoted} ]; then find {quoted} -depth -type d -empty -delete; fi"
        "  # remove emptied old folders"
    )
    return "\n".join(lines)


def legacy_layout_message(root: str | Path, moves: list[LayoutMove]) -> str:
    """Operator-facing refusal with the reason and the exact commands."""

    return (
        "This instance still has files in the old single-workspace layout, so it "
        "will not start. Control files now live in control/, databases in dbs/, "
        "runtime records in state/, and agent files in workspaces/ (users/, team/, "
        "agents/). Earlier agent files move to workspaces/team/ because their owner "
        "is not recorded; people can move private files into their own folders "
        f"later. Reason code: {LEGACY_LAYOUT_PRESENT}.\n\n"
        + render_move_commands(root, moves)
    )


# Location settings that the derived layout replaced, with the value each had by
# default. Upgrading drops a setting that still holds its default; any other
# value stops the upgrade, because the files it names must be moved by hand.
# ``None`` marks a setting whose default was "derive it".
_INTERNAL_WEBSITE_SETTINGS = ("interactive_apps", "plugins", "builtin.internal_website", "settings")
REMOVED_LOCATION_SETTINGS: dict[tuple[str, ...], str | None] = {
    ("workspace", "skills_dir"): "skills",
    ("workspace", "python_envs_dir"): "python_envs",
    ("workspace", "llm_override_path"): "LLM_OVERRIDES.yaml",
    ("workspace", "team_templates_path"): "TEAM_TEMPLATES.yaml",
    ("workspace", "team_composition_path"): "TEAM_AGENTS.yaml",
    ("agent", "inner_loop_context", "offload_dir"): "artifacts/tool_results",
    ("agent", "debug", "context_dump", "output_dir"): "artifacts/context_dumps",
    ("llm", "failover", "state_path"): None,
    ("execution_control", "paths", "artifact_store_dir"): "runtime/execution-artifacts",
    ("execution_control", "paths", "run_dir"): "runtime/execution-runs",
    (*_INTERNAL_WEBSITE_SETTINGS, "sites_dir"): "artifacts/single_page_sites",
    (*_INTERNAL_WEBSITE_SETTINGS, "attachments", "cache_dir"): "artifacts/web_chat_attachments",
    # Removed 2026-10 (InstanceLayout.mcp_connections_dir and mcp_hosting_*_dir).
    ("mcp", "registry_dir"): "runtime/mcp-connections",
    ("mcp", "hosting", "definitions_dir"): "runtime/mcp-hosting/definitions",
    ("mcp", "hosting", "state_dir"): "runtime/mcp-hosting/state",
}


def _lookup(config: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = config
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False, None
        node = node[key]
    return True, node


def present_removed_location_settings(config: Mapping[str, Any]) -> list[str]:
    """Dotted names of removed location settings still present in ``config``."""

    return [
        ".".join(path)
        for path in REMOVED_LOCATION_SETTINGS
        if _lookup(config, path)[0]
    ]


def drop_removed_location_settings(config: dict[str, Any]) -> None:
    """Remove settings that hold their former default; refuse customized ones."""

    # Callers pass a shallow copy; copy the touched sections before editing them.
    for section in {path[0] for path in REMOVED_LOCATION_SETTINGS}:
        if isinstance(config.get(section), Mapping):
            config[section] = copy.deepcopy(dict(config[section]))
    customized = []
    for path, default in REMOVED_LOCATION_SETTINGS.items():
        present, value = _lookup(config, path)
        if not present:
            continue
        if value is not None and str(value).strip() not in {"", str(default or "")}:
            customized.append(f"{'.'.join(path)}={value}")
            continue
        parents: list[Any] = [config]
        for key in path[:-1]:
            parents.append(parents[-1][key])
        del parents[-1][path[-1]]
        # A section emptied by the removal (for example execution_control.paths)
        # is removed too, so strict models do not see an unknown empty key.
        for depth in range(len(path) - 1, 0, -1):
            if parents[depth]:
                break
            del parents[depth - 1][path[depth - 1]]
    if customized:
        raise ValueError(
            "These settings point to custom locations, which are no longer "
            f"configurable: {', '.join(customized)}. Every location is now derived "
            "from the instance root (control/, dbs/, state/, runtime/, workspaces/). Move the "
            "files they name to the matching place, remove the settings, and upgrade "
            "again."
        )
