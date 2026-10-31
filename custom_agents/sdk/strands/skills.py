"""
Helpers for wiring Strands' official AgentSkills plugin into this runtime.
"""

from __future__ import annotations

from custom_tools.runtime_context import bound_task_id
import logging
import json
from collections.abc import Collection
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from default_values import AGENT_DEFAULTS

try:
    from strands import AgentSkills, Skill, tool
    from strands.types.tools import ToolContext
except ImportError:  # pragma: no cover - exercised via explicit error path
    AgentSkills = None  # type: ignore[assignment]
    Skill = None  # type: ignore[assignment]
    ToolContext = Any  # type: ignore[assignment,misc]

    def tool(*_args: Any, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return lambda function: function


if TYPE_CHECKING:
    from strands import AgentSkills as AgentSkillsType, Skill as SkillType


_LOGGER = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_RUNTIME_SKILL_ROOT = (
    _PROJECT_ROOT / "custom_agents" / "skill_templates"
).resolve()
_LOGGED_SKILL_SOURCE_SHADOWS: set[tuple[str, str, str]] = set()


def _is_bundled_runtime_skill_path(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved == _BUNDLED_RUNTIME_SKILL_ROOT or (
        _BUNDLED_RUNTIME_SKILL_ROOT in resolved.parents
    )


def _is_bundled_runtime_skill(skill: Any) -> bool:
    """Return whether a skill lives in the packaged runtime-only skill tree."""

    if skill.path is None:
        return False
    return _is_bundled_runtime_skill_path(Path(skill.path))


class PromptBoundedAgentSkills(AgentSkills if AgentSkills is not None else object):  # type: ignore[misc]
    """AgentSkills variant that bounds only the model-visible skill description menu."""

    def __init__(
        self,
        *args: Any,
        prompt_skill_description_limit: int = AGENT_DEFAULTS.prompt_skill_description_limit.value,
        excluded_skill_names: Collection[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._logged_prompt_duplicate_keys: set[str] = set()
        self.configure_prompt_descriptions(
            limit=prompt_skill_description_limit,
            excluded_skill_names=excluded_skill_names,
        )

    def configure_prompt_descriptions(
        self,
        *,
        limit: int,
        excluded_skill_names: Collection[str] | None = None,
    ) -> None:
        self.prompt_skill_description_limit = max(1, int(limit))
        self.prompt_skill_description_exclusions = {
            name.casefold()
            for name in _normalize_excluded_skill_names(excluded_skill_names)
        }

    def _generate_skills_xml(self, agent: Any = None) -> str:
        skills_by_semantic_name: dict[str, Any] = {}
        for skill in self._skills_for(agent).values():
            semantic_key = str(skill.name).strip().casefold()
            if not semantic_key:
                continue
            if semantic_key in skills_by_semantic_name:
                if semantic_key not in self._logged_prompt_duplicate_keys:
                    _LOGGER.warning(
                        "Deduplicating AgentSkills prompt descriptions for semantic skill name %s",
                        skill.name,
                    )
                    self._logged_prompt_duplicate_keys.add(semantic_key)
                # Later sources are the effective prompt description for a collision.
                skills_by_semantic_name.pop(semantic_key)
            skills_by_semantic_name[semantic_key] = skill

        visible_skills = [
            skill
            for semantic_key, skill in skills_by_semantic_name.items()
            if semantic_key not in self.prompt_skill_description_exclusions
        ]
        described_skills = visible_skills[: self.prompt_skill_description_limit]
        if not described_skills:
            return "<available_skills>\nNo skills are currently listed in the prompt menu.\n</available_skills>"

        lines = ["<available_skills>"]
        for skill in described_skills:
            lines.append("<skill>")
            lines.append(f"<name>{escape(skill.name)}</name>")
            lines.append(f"<description>{escape(skill.description)}</description>")
            if skill.path is not None and not _is_bundled_runtime_skill(skill):
                lines.append(
                    f"<location>{escape(str(skill.path / 'SKILL.md'))}</location>"
                )
            elif _is_bundled_runtime_skill(skill):
                lines.append(
                    "<activation_note>Activate with the skills tool. The activation response "
                    "contains the complete bundled instructions; do not read a package path with shell.</activation_note>"
                )
            lines.append("</skill>")
        if len(visible_skills) > len(described_skills):
            lines.append(
                "<menu_truncated>"
                f"Showing {len(described_skills)} of {len(visible_skills)} deduplicated skill descriptions. "
                "Skills omitted from this menu remain activatable when referenced by name."
                "</menu_truncated>"
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    async def _format_skill_response(self, skill: Any, sandbox: Any) -> str:
        """Keep runtime-only package paths out of model-visible skill responses."""

        if not _is_bundled_runtime_skill(skill):
            return await super()._format_skill_response(skill, sandbox)
        if not skill.instructions:
            return f"Skill '{skill.name}' activated (no instructions available)."

        parts: list[str] = [_complete_bundled_skill_instructions(skill)]
        metadata_lines: list[str] = []
        if skill.allowed_tools:
            metadata_lines.append(f"Allowed tools: {', '.join(skill.allowed_tools)}")
        if skill.compatibility:
            metadata_lines.append(f"Compatibility: {skill.compatibility}")
        metadata_lines.append(
            "Bundled instructions: complete. Do not use file or shell tools to reread "
            "the runtime package path; reactivate this skill by name if the instructions "
            "are needed again."
        )
        parts.append("\n---\n" + "\n".join(metadata_lines))
        return "\n".join(parts)

    @tool(context=True)
    async def skills(self, skill_name: str, tool_context: ToolContext) -> str:
        """Load a skill's instructions (after checking an installed skill's integrity)."""

        sdk_agent = tool_context.agent
        # Strands intentionally clears its per-agent filesystem cache when
        # set_available_skills() changes the source list, then normally reloads
        # that cache at the next outer invocation. Reviewed installation happens
        # inside an invocation and immediately directs the model to activate the
        # installed skill, so reload the deferred paths on demand here as well.
        # Without this, every filesystem skill appears absent for the remainder
        # of the installing turn even though the atomic install succeeded.
        if sdk_agent not in self._agent_skills:
            await self._load_skill_paths(sdk_agent)
        skills = self._skills_for(sdk_agent)
        if not skill_name:
            available = ", ".join(skills)
            return f"Error: skill_name is required. Available skills: {available}"
        found = skills.get(skill_name)
        if found is None:
            available = ", ".join(skills)
            return f"Skill '{skill_name}' not found. Available skills: {available}"

        from custom_tools.runtime_context import get_agent_ref

        # Skills grant nothing (access plan section 4.3): loading one needs no
        # decision. An installed skill must still match its installation.
        runtime_agent = get_agent_ref()
        if runtime_agent is not None and found.path is not None:
            from custom_agents.skill_installation import verify_managed_skill_attestation

            try:
                verify_managed_skill_attestation(runtime_agent, Path(found.path))
            except Exception as exc:
                _LOGGER.warning(
                    "Managed skill activation failed integrity verification. "
                    "event=managed_skill_integrity_failed skill_name=%s path=%s error=%s",
                    found.name,
                    found.path,
                    exc,
                )
                return json.dumps(
                    {
                        "error": "Managed skill failed installation-integrity verification.",
                        "reason_code": "managed_skill_integrity_failed",
                        "skill_name": found.name,
                    }
                )

        _LOGGER.debug("skill_name=<%s> | skill activated", skill_name)
        self._track_activated_skill(sdk_agent, skill_name)
        await _record_objective_work_skill_activation(runtime_agent, skill_name)
        return await self._format_skill_response(found, sdk_agent.sandbox)


def is_agent_skills_plugin(plugin: Any) -> bool:
    return AgentSkills is not None and isinstance(plugin, AgentSkills)


def _complete_bundled_skill_instructions(skill: Any) -> str:
    """Inline trusted bundled references because package paths stay model-hidden."""

    instructions = str(skill.instructions or "")
    if skill.path is None:
        return instructions
    references_dir = Path(skill.path) / "references"
    if not references_dir.is_dir():
        return instructions
    sections = [instructions]
    for reference_path in sorted(references_dir.glob("*.md")):
        reference_text = reference_path.read_text(encoding="utf-8").strip()
        if reference_text:
            title = reference_path.stem.replace("-", " ").title()
            sections.append(f"---\n\n# Bundled Reference: {title}\n\n{reference_text}")
    return "\n\n".join(sections)


async def _record_objective_work_skill_activation(
    runtime_agent: Any, skill_name: str
) -> None:
    if runtime_agent is None:
        return
    task_manager = getattr(runtime_agent, "task_manager", None)
    task_id = str(
        bound_task_id() or ""
    ).strip()
    coordinator = getattr(task_manager, "objective_work_coordinator", None)
    if task_id and coordinator is not None:
        await task_manager.operations_runtime.run(
            coordinator.record_skill_activation, task_id, skill_name
        )


def list_agent_skill_names(plugin: Any, sdk_agent: Any) -> list[str]:
    """Return the effective semantic skill names exposed by one SDK plugin."""

    if not is_agent_skills_plugin(plugin):
        return []
    names_by_key: dict[str, str] = {}
    for skill in plugin.get_available_skills(sdk_agent):
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            continue
        semantic_key = name.casefold()
        if semantic_key in names_by_key:
            names_by_key.pop(semantic_key)
        names_by_key[semantic_key] = name
    return sorted(names_by_key.values(), key=str.casefold)


def resolve_skills_dir(skills_dir: str | None = None) -> Path:
    """The team skills folder: control/skills, or an explicit absolute folder."""
    if skills_dir is not None:
        explicit_path = Path(skills_dir).expanduser()
        if explicit_path.is_absolute():
            return explicit_path.resolve()

    # Keep the full application configuration dependency lazy. Dedicated
    # adapters such as the Skills MCP server pass an absolute operator-owned
    # directory and should not need to package the gateway/storage stack.
    from config_loader import get_config, resolve_control_relative_path, instance_layout

    config = get_config()
    instance_root = config.get("agent.instance_root")
    if skills_dir:
        return Path(resolve_control_relative_path(skills_dir, instance_root=instance_root))
    return instance_layout(instance_root).skills_dir


def _require_agent_skills() -> Any:
    if AgentSkills is None:
        raise ImportError(
            "Strands skills support requires `strands-agents>=1.30.0`. "
            "Install or upgrade the SDK to use the official AgentSkills plugin."
        )
    return AgentSkills


def _require_skill_type() -> Any:
    if Skill is None:
        raise ImportError(
            "Strands skills support requires `strands-agents>=1.30.0`. "
            "Install or upgrade the SDK to use the official Skill API."
        )
    return Skill


def _normalize_excluded_skill_names(
    excluded_skill_names: Collection[str] | None,
) -> set[str]:
    return {
        skill_name.strip()
        for skill_name in (excluded_skill_names or [])
        if skill_name and skill_name.strip()
    }


def _load_skills_from_path(
    resolved_dir: Path, *, strict: bool = False
) -> list[SkillType]:
    skill_cls = _require_skill_type()
    if not resolved_dir.exists():
        return []
    if resolved_dir.is_dir() and (
        (resolved_dir / "SKILL.md").is_file() or (resolved_dir / "skill.md").is_file()
    ):
        return [skill_cls.from_file(resolved_dir, strict=strict)]
    if resolved_dir.is_dir():
        return list(skill_cls.from_directory(resolved_dir, strict=strict))
    if resolved_dir.is_file() and resolved_dir.name.lower() == "skill.md":
        return [skill_cls.from_file(resolved_dir, strict=strict)]
    return []


def _resolve_additional_skill_directories(
    additional_skill_directories: Collection[str] | None = None,
) -> list[Path]:
    resolved_directories: list[Path] = []
    for raw_path in additional_skill_directories or []:
        if not raw_path:
            continue
        resolved_path = Path(raw_path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = (_PROJECT_ROOT / resolved_path).resolve()
        resolved_directories.append(resolved_path)
    return resolved_directories


def _expand_skill_source_path(
    source_path: Path,
    *,
    strict: bool,
) -> list[tuple[str, Path]]:
    """Resolve one source to semantic skill names without adding them to an agent."""

    try:
        skills = _load_skills_from_path(source_path, strict=strict)
    except Exception as exc:
        _LOGGER.warning(
            "Ignoring skill source that could not be inspected before precedence resolution. "
            "event=skill_source_inspection_failed source=%s error=%s",
            source_path,
            exc,
        )
        return []

    expanded: list[tuple[str, Path]] = []
    for skill in skills:
        name = str(getattr(skill, "name", "") or "").strip()
        skill_path = getattr(skill, "path", None)
        if not name or skill_path is None:
            continue
        expanded.append((name.casefold(), Path(skill_path).expanduser().resolve()))
    return expanded


def _log_shadowed_skill_source(
    *,
    semantic_name: str,
    selected_source: Path,
    ignored_source: Path,
) -> None:
    key = (semantic_name, str(selected_source), str(ignored_source))
    if key in _LOGGED_SKILL_SOURCE_SHADOWS:
        return
    _LOGGED_SKILL_SOURCE_SHADOWS.add(key)
    if _is_bundled_runtime_skill_path(ignored_source):
        _LOGGER.info(
            "External skill takes precedence over bundled fallback. "
            "event=bundled_skill_shadowed skill_name=%s selected_source=%s ignored_source=%s",
            semantic_name,
            selected_source,
            ignored_source,
        )
        return
    _LOGGER.warning(
        "Ignoring lower-priority duplicate skill source. "
        "event=duplicate_skill_source_ignored skill_name=%s selected_source=%s ignored_source=%s",
        semantic_name,
        selected_source,
        ignored_source,
    )


def resolve_skill_sources(
    *,
    skills_dir: str | None = None,
    additional_skill_directories: Collection[str] | None = None,
    strict: bool = False,
) -> list[str]:
    source_paths = [
        resolve_skills_dir(skills_dir),
        *_resolve_additional_skill_directories(additional_skill_directories),
    ]

    # Workspace and configured sources are authoritative. Package-owned
    # templates are deployable fallbacks and must never overwrite or coexist
    # with an external skill that has the same semantic name.
    ordered_paths = [
        path for path in source_paths if not _is_bundled_runtime_skill_path(path)
    ] + [path for path in source_paths if _is_bundled_runtime_skill_path(path)]

    selected_by_name: dict[str, Path] = {}
    selected_paths: list[str] = []
    seen_paths: set[str] = set()
    for source_path in ordered_paths:
        for semantic_name, expanded_path in _expand_skill_source_path(
            source_path,
            strict=strict,
        ):
            path_key = str(expanded_path)
            if semantic_name in selected_by_name:
                _log_shadowed_skill_source(
                    semantic_name=semantic_name,
                    selected_source=selected_by_name[semantic_name],
                    ignored_source=expanded_path,
                )
                continue
            if path_key in seen_paths:
                continue
            selected_by_name[semantic_name] = expanded_path
            seen_paths.add(path_key)
            selected_paths.append(path_key)

    return selected_paths


def _iter_skill_files(resolved_dir: Path) -> list[Path]:
    if not resolved_dir.exists():
        return []
    if resolved_dir.is_file():
        return [resolved_dir]
    return [path for path in sorted(resolved_dir.rglob("*")) if path.is_file()]


def get_skills_fingerprint(
    skills_dir: str | None = None,
    additional_skill_directories: Collection[str] | None = None,
    *,
    strict: bool = False,
) -> tuple[tuple[str, int, int], ...]:
    """
    Build a stable fingerprint for the skills directory contents.

    This lets the runtime avoid reparsing skills on every invocation while still
    picking up new or modified skills without a restart.
    """
    fingerprint = []
    for source_path in resolve_skill_sources(
        skills_dir=skills_dir,
        additional_skill_directories=additional_skill_directories,
        strict=strict,
    ):
        resolved_dir = Path(source_path)
        for file_path in _iter_skill_files(resolved_dir):
            stat = file_path.stat()
            relative_path = (
                file_path.relative_to(resolved_dir).as_posix()
                if resolved_dir.is_dir()
                else file_path.name
            )
            fingerprint.append(
                (f"{resolved_dir}:{relative_path}", stat.st_mtime_ns, stat.st_size)
            )
    return tuple(fingerprint)


def build_skills_plugin(
    *,
    skills_dir: str | None = None,
    additional_skill_directories: Collection[str] | None = None,
    excluded_skill_names: Collection[str] | None = None,
    strict: bool = False,
    max_resource_files: int = 20,
    prompt_skill_description_limit: int = AGENT_DEFAULTS.prompt_skill_description_limit.value,
) -> AgentSkillsType:
    """Create an AgentSkills plugin rooted at the configured skills directory."""
    _require_agent_skills()
    plugin = PromptBoundedAgentSkills(
        skills=resolve_skill_sources(
            skills_dir=skills_dir,
            additional_skill_directories=additional_skill_directories,
            strict=strict,
        ),
        strict=strict,
        max_resource_files=max_resource_files,
        prompt_skill_description_limit=prompt_skill_description_limit,
        excluded_skill_names=excluded_skill_names,
    )
    return plugin


def refresh_skills_plugin(
    plugin: AgentSkillsType,
    *,
    skills_dir: str | None = None,
    additional_skill_directories: Collection[str] | None = None,
    excluded_skill_names: Collection[str] | None = None,
    prompt_skill_description_limit: int = AGENT_DEFAULTS.prompt_skill_description_limit.value,
    force: bool = False,
    previous_fingerprint: Optional[tuple[tuple[str, int, int], ...]] = None,
) -> tuple[tuple[str, int, int], ...]:
    """
    Re-scan filesystem-backed skills when the underlying directory changes.

    AgentSkills refreshes its XML prompt injection before each invocation, but it
    does not automatically re-read filesystem-backed sources. This helper keeps
    the plugin in sync with disk without reparsing when nothing changed.
    """
    current_fingerprint = get_skills_fingerprint(
        skills_dir=skills_dir,
        additional_skill_directories=additional_skill_directories,
        strict=bool(getattr(plugin, "_strict", False)),
    )
    if not force and previous_fingerprint == current_fingerprint:
        return current_fingerprint

    skill_sources = resolve_skill_sources(
        skills_dir=skills_dir,
        additional_skill_directories=additional_skill_directories,
        strict=bool(getattr(plugin, "_strict", False)),
    )
    plugin.set_available_skills(skill_sources)
    if isinstance(plugin, PromptBoundedAgentSkills):
        plugin.configure_prompt_descriptions(
            limit=prompt_skill_description_limit,
            excluded_skill_names=excluded_skill_names,
        )
    _LOGGER.debug("Refreshed Strands skills from %s", ", ".join(skill_sources))
    return current_fingerprint


def list_skill_metadata(
    *,
    skills_dir: str | None = None,
    excluded_skill_names: Collection[str] | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return available skills as simple metadata records."""
    resolved_dir = resolve_skills_dir(skills_dir)
    excluded = {
        name.casefold()
        for name in _normalize_excluded_skill_names(excluded_skill_names)
    }
    metadata = []
    for skill in _load_skills_from_path(resolved_dir, strict=strict):
        if skill.name.casefold() in excluded:
            continue
        metadata.append(
            {
                "name": skill.name,
                "description": skill.description,
                "location": str(skill.path / "SKILL.md")
                if skill.path is not None
                else None,
            }
        )
    return metadata


def describe_skill(
    skill_name: str,
    *,
    skills_dir: str | None = None,
    strict: bool = False,
    max_resource_files: int = 20,
) -> dict[str, Any]:
    """Return a detailed description of one filesystem-backed skill."""
    resolved_dir = resolve_skills_dir(skills_dir)
    for skill in _load_skills_from_path(resolved_dir, strict=strict):
        if skill.name != skill_name:
            continue

        resource_files: list[dict[str, str]] = []
        if skill.path is not None:
            for resource_path in _iter_skill_files(skill.path):
                try:
                    relative_path = resource_path.relative_to(skill.path).as_posix()
                except ValueError:
                    continue
                if not relative_path.startswith(
                    ("scripts/", "references/", "reference/", "assets/")
                ):
                    continue
                resource_files.append(
                    {
                        "relative_path": relative_path,
                        "absolute_path": str(resource_path.resolve()),
                    }
                )
                if len(resource_files) >= max_resource_files:
                    break

        return {
            "skill_name": skill.name,
            "description": skill.description,
            "skill_root": str(skill.path.resolve()) if skill.path is not None else None,
            "skill_markdown_path": str((skill.path / "SKILL.md").resolve())
            if skill.path is not None
            else None,
            "allowed_tools": list(skill.allowed_tools or []),
            "metadata": dict(skill.metadata or {}),
            "compatibility": skill.compatibility,
            "license": skill.license,
            "skill_instructions_markdown": skill.instructions,
            "bundled_resources": resource_files,
        }

    raise FileNotFoundError(f"Skill '{skill_name}' not found under {resolved_dir}")
