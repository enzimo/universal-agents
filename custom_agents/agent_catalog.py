"""YAML-backed agent catalog loading and tool hydration.

Agent definition YAML is intentionally declarative. It names the tools a type
works with as tool catalog ids or tool set ids (``default_tools``; see
``custom_agents.default_tools`` for what they mean in each authorization
mode); it cannot import Python callables. An agent's hydrated ``tools`` are
the Strands functions of its role's standard tool sets plus those of its
default tools (``access_catalog.tool_functions``). Access is decided on every
call by the access service.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from custom_agents.agent_instructions import (
    shared_agent_base_prompt,
    team_backstory_hubnspoke,
)
from custom_messages.contracts.runtime_v1 import (
    MEM_REVIEW_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from runtime_settings.instance_layout import (
    AGENT_BUNDLES_DIRNAME,
    AGENT_DEFINITIONS_DIRNAME,
    InstanceLayout,
)
from custom_messages.contracts.schedule_worker_v1 import (
    DEFINITION_SOURCE_RELEASE,
    DEFINITION_SOURCE_TEAM,
    AgentDefinitionSource,
)
from custom_tools.agent_catalog_port import (
    AgentTypeCatalogSummary,
    configure_agent_catalog_lookup,
)
from access_catalog.tool_functions import functions_of_tool_sets, functions_of_tools
from access_control.entities import AGENT_ROLE_SYSTEM, AGENT_ROLE_TEAM_LEAD, AGENT_ROLE_WORKER
from access_control.system_agents import RELEASE_DEFINITION_ROOT
from access_control.standard_grants import INTERNET_SET, STANDARD_TOOL_SETS_BY_ROLE
from custom_agents.default_tools import builtin_tool_catalog, default_tool_ids
from custom_tools.provider_contract import tool_name
from custom_tools.registry import ALL_PROVIDER_TOOLS


# Definitions under this folder ship with the installed code and change only
# with a deploy. Every other definition file is team-written (see
# definition_source); scheduled workers treat the two differently.
BUILTIN_AGENT_CATALOG_PATH = (
    RELEASE_DEFINITION_ROOT / "agent_definitions" / "builtins.yaml"
)
BUILTIN_AGENT_BUNDLE_ROOT = RELEASE_DEFINITION_ROOT / "agent_bundles"
# Team-written definitions live in control/, which only trusted provisioning
# and operators write (runtime_settings.instance_layout).
TEAM_AGENT_DEFINITION_DIRS = (AGENT_DEFINITIONS_DIRNAME, AGENT_BUNDLES_DIRNAME)
BUNDLE_CATALOG_FILENAMES = ("agent.yaml", "agents.yaml", "catalog.yaml")
_TEMPLATE_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
# Fields replaced by default_tools (access plan Phase 7). A definition that
# still uses them is refused when it loads, with the fix in the message.
REMOVED_TOOL_FIELDS = ("tool_capabilities", "capability_registry_ids")


class AgentOnboardingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = True
    locked: bool = False
    default_enabled: bool = False
    default_count: int = 1
    summary: str = ""
    description: str = ""

    @field_validator("default_count")
    @classmethod
    def _validate_default_count(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 0 or parsed > 32:
            raise ValueError("onboarding.default_count must be between 0 and 32.")
        return parsed


class AgentTemplateParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "string"
    default: Any = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        normalized = str(value or "string").strip().lower()
        if normalized not in {"string", "integer", "boolean"}:
            raise ValueError(
                "template parameter type must be one of: string, integer, boolean."
            )
        return normalized


class AgentTemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: dict[str, AgentTemplateParameter] = Field(default_factory=dict)
    definition: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def _validate_parameter_names(
        cls,
        value: dict[str, AgentTemplateParameter],
    ) -> dict[str, AgentTemplateParameter]:
        normalized: dict[str, AgentTemplateParameter] = {}
        for name, parameter in value.items():
            rendered = str(name or "").strip()
            if not rendered:
                raise ValueError("agent template parameters must have non-empty names.")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rendered):
                raise ValueError(f"invalid agent template parameter name: {rendered!r}")
            normalized[rendered] = parameter
        return normalized

    @field_validator("definition")
    @classmethod
    def _require_definition_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("agent template definition must be a non-empty mapping.")
        return value


class AgentTemplateUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extends: str
    params: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extends")
    @classmethod
    def _require_extends(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("templated agent definitions must set extends.")
        return normalized


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    role: str
    description: str
    prompt: str | None = None
    prompt_ref: str | None = None
    include_shared_prompt: bool = True
    backstory: str = "hub_and_spoke"
    # Tool catalog ids or tool set ids this type works with (module docstring).
    # Checked against the catalog when the definition is hydrated.
    default_tools: list[str] = Field(default_factory=list)
    provisioning_record_ref: str | None = None
    skills_enabled: bool = True
    # "system" marks built-in maintenance agents (fixed duties, work with no
    # person; access_control.standard_grants). Only release definitions may
    # set it (checked when the catalog is hydrated).
    access_role: Literal["worker", "system"] = "worker"
    onboarding: AgentOnboardingDefinition = Field(
        default_factory=AgentOnboardingDefinition
    )

    @field_validator("display_name", "role", "description")
    @classmethod
    def _require_non_empty_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("agent text fields must be non-empty.")
        return normalized

    @field_validator("default_tools")
    @classmethod
    def _normalize_default_tools(cls, value: list[str]) -> list[str]:
        return sorted({str(item).strip() for item in value if str(item).strip()})

    @field_validator("provisioning_record_ref")
    @classmethod
    def _validate_provisioning_record_ref(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if normalized != "provisioning.json":
            raise ValueError(
                "provisioning_record_ref must be exactly 'provisioning.json'."
            )
        return normalized

    @model_validator(mode="after")
    def _require_generated_defaults(self) -> "AgentDefinition":
        # A generated specialist's record binds the default tools a person
        # approved (custom_agents.generated_agent_runtime).
        if self.provisioning_record_ref and not self.default_tools:
            raise ValueError("provisioning_record_ref requires default_tools.")
        return self


class TeamTemplateMemberDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_type: str
    count: int = 1
    mcp_profile_names: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("agent_type")
    @classmethod
    def _require_agent_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("team template member agent_type must be non-empty.")
        return normalized

    @field_validator("count")
    @classmethod
    def _validate_count(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 1 or parsed > 32:
            raise ValueError("team template member count must be between 1 and 32.")
        return parsed


class TeamTemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    members: list[TeamTemplateMemberDefinition] = Field(default_factory=list)


class AgentCatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    agent_templates: dict[str, AgentTemplateDefinition] = Field(default_factory=dict)
    agent_types: dict[str, dict[str, Any]] = Field(default_factory=dict)
    team_templates: dict[str, TeamTemplateDefinition] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != 1:
            raise ValueError("Only agent definition schema_version 1 is supported.")
        return int(value)

    @field_validator("agent_types", mode="before")
    @classmethod
    def _validate_agent_types_mapping(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("agent_types must be a mapping.")
        for agent_type, definition in value.items():
            if not str(agent_type or "").strip():
                raise ValueError("agent_types contains an empty agent type.")
            if not isinstance(definition, dict):
                raise ValueError(f"agent_types.{agent_type} must be a mapping.")
        return value

    @field_validator("agent_templates")
    @classmethod
    def _validate_template_names(
        cls,
        value: dict[str, AgentTemplateDefinition],
    ) -> dict[str, AgentTemplateDefinition]:
        for template_name in value:
            if not str(template_name or "").strip():
                raise ValueError("agent_templates contains an empty template name.")
        return value


@dataclass(frozen=True)
class HydratedAgentDefinition:
    named_agent_type: str
    display_name: str
    role: str
    description: str
    prompt: str
    backstory: str
    tools: tuple[Any, ...]
    default_tools: tuple[str, ...]
    provisioning_record_path: str | None
    source_path: str
    skills_enabled: bool
    onboarding: AgentOnboardingDefinition
    definition_source: AgentDefinitionSource
    access_role: str
    # What the definition file declares, before the running code expands its
    # role's standard tools and default tools, or prepends the shared prompt.
    declared: dict[str, Any]

    def to_runtime_config(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "goal": self.prompt,
            "backstory": self.backstory,
            "tools": list(self.tools),
            "default_tools": list(self.default_tools),
            "provisioning_record_path": self.provisioning_record_path,
            "agent_definition_source_path": self.source_path,
            "agent_definition_source": self.definition_source,
            "declared_definition": dict(self.declared),
            "class_name": "UniversalAgent",
            "named_agent_type": self.named_agent_type,
            "skills_enabled": self.skills_enabled,
            "access_role": self.access_role,
            "display_name": self.display_name,
            "description": self.description,
            "onboarding": self.onboarding.model_dump(mode="json"),
        }

    def to_onboarding_payload(self) -> dict[str, Any]:
        return {
            "type": self.named_agent_type,
            "display_name": self.display_name,
            "summary": self.onboarding.summary or self.role,
            "description": self.onboarding.description or self.description,
            "locked": self.onboarding.locked,
            "default_count": self.onboarding.default_count,
            "default_enabled": self.onboarding.default_enabled,
            "visible": self.onboarding.visible,
        }


@dataclass(frozen=True)
class HydratedAgentCatalog:
    agent_types: dict[str, HydratedAgentDefinition]
    team_templates: dict[str, TeamTemplateDefinition]
    source_paths: tuple[Path, ...]

    def to_agent_type_map(self) -> dict[str, dict[str, Any]]:
        return {
            agent_type: definition.to_runtime_config()
            for agent_type, definition in self.agent_types.items()
        }

    def onboarding_agents(self) -> list[dict[str, Any]]:
        return [
            definition.to_onboarding_payload()
            for definition in self.agent_types.values()
            if definition.onboarding.visible
        ]

    def startup_agent_types(self) -> tuple[str, ...]:
        return tuple(
            agent_type
            for agent_type, definition in self.agent_types.items()
            if agent_type != TEAM_ARCHITECT_AGENT_TYPE
        )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Agent catalog file must contain a YAML mapping: {path}")
    return raw


def _catalog_files_from_path(path: Path) -> list[Path]:
    if path.is_dir():
        bundle_catalogs = [
            path / filename
            for filename in BUNDLE_CATALOG_FILENAMES
            if (path / filename).is_file()
        ]
        if len(bundle_catalogs) > 1:
            names = ", ".join(item.name for item in bundle_catalogs)
            raise ValueError(
                f"Agent bundle may contain only one catalog file: {path} ({names})"
            )
        if bundle_catalogs:
            return bundle_catalogs
        yaml_files = sorted(
            [
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() in {".yaml", ".yml"}
            ]
        )
        if yaml_files:
            return yaml_files
        nested_bundle_catalogs: list[Path] = []
        for item in sorted(
            [
                child
                for child in path.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ]
        ):
            nested_bundle_catalogs.extend(_catalog_files_from_path(item))
        return nested_bundle_catalogs
    return [path]


def _builtin_agent_bundle_paths() -> list[Path]:
    if not BUILTIN_AGENT_BUNDLE_ROOT.exists():
        return []
    return sorted(
        [
            item
            for item in BUILTIN_AGENT_BUNDLE_ROOT.iterdir()
            if item.is_dir()
            or (item.is_file() and item.suffix.lower() in {".yaml", ".yml"})
        ]
    )


def _control_dir(instance_root: str | None) -> Path:
    return InstanceLayout.at(instance_root or ".").control_dir


def _resolve_catalog_path(raw_path: str, *, instance_root: str | None) -> Path:
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = _control_dir(instance_root) / path
    return path.resolve()


def resolve_agent_definition_paths(
    raw_paths: Iterable[str] | str | None,
    *,
    instance_root: str | None,
) -> list[Path]:
    """Operator-listed definition paths; relative ones resolve under control/."""

    if raw_paths is None:
        return []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    return [
        _resolve_catalog_path(str(path), instance_root=instance_root)
        for path in raw_paths
        if str(path or "").strip()
    ]


def default_team_agent_definition_paths(*, instance_root: str | None) -> list[Path]:
    control = _control_dir(instance_root)
    return [
        path
        for path in (control / dirname for dirname in TEAM_AGENT_DEFINITION_DIRS)
        if path.exists()
    ]


def _configured_instance_root() -> str | None:
    import config_loader

    config = getattr(config_loader, "_config", None)
    return config.get("agent.instance_root") if config is not None else None


def configured_agent_definition_paths() -> list[Path]:
    try:
        import config_loader

        config = getattr(config_loader, "_config", None)
        if config is None:
            return []
        raw_paths = config.get("agent.agent_definition_paths", []) or []
        instance_root = config.get("agent.instance_root")
    except Exception:
        raw_paths = []
        instance_root = None

    return resolve_agent_definition_paths(raw_paths, instance_root=instance_root)


def _load_catalog_documents(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> list[tuple[Path, AgentCatalogDocument]]:
    paths: list[Path] = [BUILTIN_AGENT_CATALOG_PATH]
    if include_default_bundles:
        paths.extend(_builtin_agent_bundle_paths())
    if include_configured:
        try:
            instance_root = _configured_instance_root()
        except Exception:
            instance_root = None
        paths.extend(default_team_agent_definition_paths(instance_root=instance_root))
        paths.extend(configured_agent_definition_paths())
    for path in additional_paths or []:
        paths.append(Path(path).expanduser().resolve())

    documents: list[tuple[Path, AgentCatalogDocument]] = []
    seen: set[Path] = set()
    for root_path in paths:
        for path in _catalog_files_from_path(root_path):
            if path in seen:
                continue
            seen.add(path)
            if not path.exists():
                raise FileNotFoundError(
                    f"Agent definition catalog does not exist: {path}"
                )
            documents.append(
                (path, AgentCatalogDocument.model_validate(_read_yaml_mapping(path)))
            )
    return documents


def agent_role(agent_type: str, access_role: str) -> str:
    """The access role an agent of this type holds (``custom_agents.access_runtime``)."""

    if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
        return AGENT_ROLE_TEAM_LEAD
    return AGENT_ROLE_SYSTEM if access_role == AGENT_ROLE_SYSTEM else AGENT_ROLE_WORKER


def _hydrate_tools(agent_type: str, role: str, default_tools: Iterable[str]) -> tuple[Any, ...]:
    """The functions of the role's standard tool sets and of the default tools.

    Catalog functions that no trusted provider supplies (the skills plugin's
    ``skills``) come from elsewhere and are left out here, as at startup
    (``custom_agents.tool_sync.startup_tools``).
    """

    catalog = builtin_tool_catalog()
    names = functions_of_tool_sets(
        catalog, (*STANDARD_TOOL_SETS_BY_ROLE.get(role, ()), INTERNET_SET)
    ) | functions_of_tools(catalog, default_tool_ids(agent_type, default_tools, catalog))
    provider = {tool_name(tool): tool for tool in ALL_PROVIDER_TOOLS}
    return tuple(provider[name] for name in sorted(names) if name in provider)


def _own_prompt(definition: AgentDefinition, *, catalog_path: Path) -> str:
    """The prompt text this definition declares, without the shared base prompt."""
    prompt_parts: list[str] = []
    if definition.prompt:
        prompt_parts.append(definition.prompt.strip())
    if definition.prompt_ref:
        prompt_path = (catalog_path.parent / definition.prompt_ref).resolve()
        try:
            prompt_parts.append(prompt_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Prompt file not found for {catalog_path}: {prompt_path}"
            ) from exc
    return "\n\n".join(part for part in prompt_parts if part)


def _hydrate_prompt(own_prompt: str, definition: AgentDefinition) -> str:
    if not definition.include_shared_prompt:
        return own_prompt
    if not own_prompt:
        raise ValueError(
            f"Agent {definition.display_name} must define prompt or prompt_ref."
        )
    return "\n\n".join((shared_agent_base_prompt.strip(), own_prompt))


def _access_role(agent_type: str, definition: AgentDefinition, source_path: Path) -> str:
    """A team-written definition cannot make its agents system agents."""

    if definition.access_role == "system" and definition_source(source_path) != DEFINITION_SOURCE_RELEASE:
        raise ValueError(f"Agent {agent_type} is team-defined and cannot declare access_role: system.")
    return definition.access_role


def definition_source(source_path: Path) -> AgentDefinitionSource:
    """Label a definition file as shipped with the code or written by the team.

    A team file that redefines a built-in type name is team-written, and a team
    definition that extends a shipped template is team-written too.
    """
    resolved = source_path.resolve()
    if resolved.is_relative_to(RELEASE_DEFINITION_ROOT.resolve()):
        return DEFINITION_SOURCE_RELEASE
    return DEFINITION_SOURCE_TEAM


def _declared_definition(definition: AgentDefinition, own_prompt: str) -> dict[str, Any]:
    """Behavior-relevant fields as declared, for schedule change review.

    Display name, description and onboarding metadata are excluded: they do not
    change what a worker does. Default tools are kept as declared (tool set
    ids, not their members), so a release that adds a tool to a set does not
    count as a team edit.
    """
    return {
        "role": definition.role,
        "own_prompt": own_prompt,
        "include_shared_prompt": definition.include_shared_prompt,
        "backstory": definition.backstory,
        "default_tools": list(definition.default_tools),
        "skills_enabled": definition.skills_enabled,
    }


def _hydrate_backstory(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized == "hub_and_spoke":
        return team_backstory_hubnspoke
    return normalized


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _coerce_template_param(
    name: str, parameter: AgentTemplateParameter, value: Any
) -> Any:
    if parameter.type == "string":
        return str(value)
    if parameter.type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"agent template parameter {name!r} must be an integer."
            ) from exc
    if parameter.type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"agent template parameter {name!r} must be a boolean.")
    raise ValueError(
        f"unsupported agent template parameter type for {name!r}: {parameter.type}"
    )


def _resolve_template_params(
    *,
    template_name: str,
    template: AgentTemplateDefinition,
    supplied: dict[str, Any],
) -> dict[str, Any]:
    supplied = dict(supplied or {})
    unknown = sorted(set(supplied) - set(template.parameters))
    if unknown:
        raise ValueError(
            f"Agent template {template_name!r} received unknown params: {', '.join(unknown)}"
        )

    resolved: dict[str, Any] = {}
    for name, parameter in template.parameters.items():
        if name in supplied:
            raw_value = supplied[name]
        elif parameter.default is not None:
            raw_value = parameter.default
        else:
            raise ValueError(
                f"Agent template {template_name!r} missing required param {name!r}."
            )
        resolved[name] = _coerce_template_param(name, parameter, raw_value)
    return resolved


def _render_template_value(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full_match = _TEMPLATE_PATTERN.fullmatch(value.strip())
        if full_match:
            name = full_match.group(1)
            if name not in params:
                raise ValueError(f"Unknown agent template param {name!r}.")
            return params[name]

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in params:
                raise ValueError(f"Unknown agent template param {name!r}.")
            return str(params[name])

        return _TEMPLATE_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_render_template_value(item, params) for item in value]
    if isinstance(value, dict):
        return {
            str(_render_template_value(key, params)): _render_template_value(
                item, params
            )
            for key, item in value.items()
        }
    return value


def _refuse_removed_tool_fields(agent_type: str, definition: dict[str, Any], catalog_path: Path) -> None:
    """Tool groups and registry ids were replaced by default_tools; say how to fix the file."""

    removed = [field for field in REMOVED_TOOL_FIELDS if field in definition]
    if removed:
        raise ValueError(
            f"Agent {agent_type} in {catalog_path} uses the removed field(s) {', '.join(removed)}. "
            "List the tools it works with under default_tools as tool catalog ids or tool set ids "
            "instead (ua_definitions/agent_definitions/builtins.yaml shows examples); access comes "
            "from grants. A specialist TeamLead generated earlier cannot be carried over: move its "
            "folder out of control/agent_bundles/team_lead_generated and create it again."
        )


def _agent_definition_from_raw(
    *,
    agent_type: str,
    raw_definition: dict[str, Any],
    templates: dict[str, tuple[Path, AgentTemplateDefinition]],
    catalog_path: Path,
) -> tuple[Path, Path, AgentDefinition]:
    if "extends" not in raw_definition:
        _refuse_removed_tool_fields(agent_type, raw_definition, catalog_path)
        return (
            catalog_path,
            catalog_path,
            AgentDefinition.model_validate(raw_definition),
        )

    use = AgentTemplateUse.model_validate(raw_definition)
    template_entry = templates.get(use.extends)
    if template_entry is None:
        raise ValueError(
            f"Agent {agent_type} in {catalog_path} extends unknown template {use.extends!r}."
        )
    template_path, template = template_entry
    params = _resolve_template_params(
        template_name=use.extends,
        template=template,
        supplied=use.params,
    )
    rendered = _render_template_value(copy.deepcopy(template.definition), params)
    if not isinstance(rendered, dict):
        raise ValueError(f"Agent template {use.extends!r} did not render to a mapping.")
    rendered = _deep_merge_dicts(rendered, use.overrides)
    _refuse_removed_tool_fields(agent_type, rendered, catalog_path)
    prompt_source_path = (
        catalog_path if "prompt_ref" in use.overrides else template_path
    )
    return prompt_source_path, catalog_path, AgentDefinition.model_validate(rendered)


def load_agent_catalog(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> HydratedAgentCatalog:
    documents = _load_catalog_documents(
        additional_paths,
        include_configured=include_configured,
        include_default_bundles=include_default_bundles,
    )
    agent_documents: dict[str, tuple[Path, Path, AgentDefinition]] = {}
    templates: dict[str, tuple[Path, AgentTemplateDefinition]] = {}
    team_templates: dict[str, TeamTemplateDefinition] = {}
    source_paths: list[Path] = []

    for path, document in documents:
        source_paths.append(path)
        for template_name, template in document.agent_templates.items():
            normalized_template_name = str(template_name or "").strip()
            if not normalized_template_name:
                raise ValueError(
                    f"Agent catalog contains an empty template name: {path}"
                )
            templates[normalized_template_name] = (path, template)
        for agent_type, raw_definition in document.agent_types.items():
            normalized_agent_type = str(agent_type or "").strip()
            if not normalized_agent_type:
                raise ValueError(f"Agent catalog contains an empty agent type: {path}")
            agent_documents[normalized_agent_type] = _agent_definition_from_raw(
                agent_type=normalized_agent_type,
                raw_definition=raw_definition,
                templates=templates,
                catalog_path=path,
            )
        for template_name, template in document.team_templates.items():
            normalized_template_name = str(template_name or "").strip()
            if not normalized_template_name:
                raise ValueError(
                    f"Agent catalog contains an empty team template name: {path}"
                )
            team_templates[normalized_template_name] = template

    hydrated: dict[str, HydratedAgentDefinition] = {}
    for agent_type, (
        prompt_source_path,
        source_path,
        definition,
    ) in agent_documents.items():
        own_prompt = _own_prompt(definition, catalog_path=prompt_source_path)
        access_role = _access_role(agent_type, definition, source_path)
        hydrated[agent_type] = HydratedAgentDefinition(
            named_agent_type=agent_type,
            display_name=definition.display_name,
            role=definition.role,
            description=definition.description,
            prompt=_hydrate_prompt(own_prompt, definition),
            backstory=_hydrate_backstory(definition.backstory),
            tools=_hydrate_tools(
                agent_type,
                agent_role(agent_type, access_role),
                definition.default_tools,
            ),
            default_tools=tuple(definition.default_tools),
            provisioning_record_path=(
                str((source_path.parent / definition.provisioning_record_ref).resolve())
                if definition.provisioning_record_ref
                else None
            ),
            source_path=str(source_path),
            skills_enabled=definition.skills_enabled,
            onboarding=definition.onboarding,
            definition_source=definition_source(source_path),
            declared=_declared_definition(definition, own_prompt),
            access_role=access_role,
        )

    for template_name, template in team_templates.items():
        for member in template.members:
            if member.agent_type not in hydrated:
                raise ValueError(
                    f"Team template {template_name!r} references unknown agent type "
                    f"{member.agent_type!r}."
                )

    if TEAM_ARCHITECT_AGENT_TYPE not in hydrated:
        raise ValueError(f"Agent catalog must define {TEAM_ARCHITECT_AGENT_TYPE}.")
    if MEM_REVIEW_AGENT_TYPE not in hydrated:
        raise ValueError(f"Agent catalog must define {MEM_REVIEW_AGENT_TYPE}.")
    return HydratedAgentCatalog(
        agent_types=hydrated,
        team_templates=team_templates,
        source_paths=tuple(source_paths),
    )


def load_agent_type_map(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> dict[str, dict[str, Any]]:
    return load_agent_catalog(
        additional_paths,
        include_configured=include_configured,
        include_default_bundles=include_default_bundles,
    ).to_agent_type_map()


def get_onboarding_agent_catalog(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> list[dict[str, Any]]:
    return load_agent_catalog(
        additional_paths,
        include_configured=include_configured,
        include_default_bundles=include_default_bundles,
    ).onboarding_agents()


def get_startup_agent_types(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> tuple[str, ...]:
    return load_agent_catalog(
        additional_paths,
        include_configured=include_configured,
        include_default_bundles=include_default_bundles,
    ).startup_agent_types()


def get_default_team_templates(
    additional_paths: Iterable[str | Path] | None = None,
    *,
    include_configured: bool = True,
    include_default_bundles: bool = True,
) -> dict[str, TeamTemplateDefinition]:
    return load_agent_catalog(
        additional_paths,
        include_configured=include_configured,
        include_default_bundles=include_default_bundles,
    ).team_templates


def _lookup_agent_type_summary(
    additional_paths: Iterable[str | Path],
    *,
    agent_type: str,
    include_configured: bool,
) -> AgentTypeCatalogSummary | None:
    catalog = load_agent_catalog(
        additional_paths,
        include_configured=include_configured,
    )
    definition = catalog.agent_types.get(agent_type)
    if definition is None:
        return None
    return AgentTypeCatalogSummary(
        agent_type=agent_type,
        display_name=definition.display_name,
        role=definition.role,
        description=definition.description,
        source_paths=tuple(str(path) for path in catalog.source_paths),
        default_tools=definition.default_tools,
        runtime_config=definition.to_runtime_config(),
    )


# The catalog is the composition root for trusted tool hydration. Installing its
# narrow metadata adapter here preserves that direction without a tool back-import.
configure_agent_catalog_lookup(
    _lookup_agent_type_summary,
)
