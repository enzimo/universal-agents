"""Agent types configuration and contracts."""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field
from runtime_settings.agents import AgentRuntimeOverrideSettings
from runtime_settings.models import AgentTypeLLMOverrideSettings


class AgentTypeRuntimeSettings(BaseModel):
    agent: AgentRuntimeOverrideSettings = Field(
        default_factory=AgentRuntimeOverrideSettings
    )
    llm: Optional[AgentTypeLLMOverrideSettings] = None
