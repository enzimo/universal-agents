"""Validate common sections before projecting runtime constructor arguments.

Instance files and generic files share the same setting owners. Presence is
preserved here: model capability discovery and explicit null public ports depend
on the distinction between an absent value and an operator-supplied value.
Unknown keys belong to generic composition (identity, bootstrap, and secrets),
and remain available to that composition rather than being discarded by a model.
"""

from typing import Any

from pydantic import BaseModel

from runtime_settings.access_workflows import AccessWorkflowSettings
from runtime_settings.agent_types import AgentTypeRuntimeSettings
from runtime_settings.agents import TOOL_LIST_FIELDS, AgentRuntimeDefaultsSettings
from runtime_settings.models import InstanceEmbeddingSettings, InstanceLLMSettings
from runtime_settings.network import NetworkSettings
from runtime_settings.transport import TransportSettings
from runtime_settings.values import _mapping_or_empty
from runtime_settings.workspace import WorkspaceSettings


SECTION_MODELS: dict[str, type[BaseModel]] = {
    "access_workflows": AccessWorkflowSettings,
    "agent": AgentRuntimeDefaultsSettings,
    "embedding": InstanceEmbeddingSettings,
    "llm": InstanceLLMSettings,
    "network": NetworkSettings,
    "transport": TransportSettings,
    "workspace": WorkspaceSettings,
}


class _AgentToolListConfiguration(BaseModel):
    """Reuse the setting owners and retain the full path in validation errors."""

    agent: AgentRuntimeDefaultsSettings
    agent_types: dict[str, AgentTypeRuntimeSettings]


def validate_agent_tool_lists(sections: dict[str, Any]) -> None:
    """Check every configured list before overrides can hide an invalid entry.

    Select only tool lists: other settings retain their existing resolution
    rules, and validation must not insert defaults or erase explicit nulls.
    An unused agent type must fail here rather than at its first worker launch.
    """

    def tool_lists(raw: Any) -> dict[str, Any]:
        settings = _mapping_or_empty(raw)
        return {field: settings[field] for field in TOOL_LIST_FIELDS if field in settings}

    _AgentToolListConfiguration.model_validate({
        "agent": tool_lists(sections.get("agent")),
        "agent_types": {
            name: {"agent": tool_lists(_mapping_or_empty(settings).get("agent"))}
            for name, settings in _mapping_or_empty(sections.get("agent_types")).items()
        },
    })


def normalize_runtime_sections(sections: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(sections)
    for name, model in SECTION_MODELS.items():
        raw = dict(sections.get(name) or {})
        owned = model.model_validate(raw).model_dump(mode="json", exclude_unset=True)
        normalized[name] = {**raw, **owned}
    return normalized
