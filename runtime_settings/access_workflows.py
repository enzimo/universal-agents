"""Settings for access workflows (``custom_agents.access_workflows``).

Workflows are plans agents publish and people follow on the workflow page;
they own these limits. The two settings used to live in ``human_interactions``
as ``workflow_max_steps`` and ``workflow_diagnostic_event_limit``; that block
belonged to the old approval system and is refused by name
(``runtime_settings.governance.refuse_removed_access_settings``), naming these
keys when the old ones are still set.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from default_values import ACCESS_WORKFLOW_DEFAULTS

# Old key -> new key, for the refusal message of the removed ``human_interactions``.
MOVED_WORKFLOW_KEYS = {
    "workflow_max_steps": "access_workflows.max_steps",
    "workflow_diagnostic_event_limit": "access_workflows.diagnostic_event_limit",
}


class AccessWorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(
        default=ACCESS_WORKFLOW_DEFAULTS.max_steps.value,
        ge=1,
        le=64,
        description="Maximum steps in one access workflow.",
    )
    diagnostic_event_limit: int = Field(
        default=ACCESS_WORKFLOW_DEFAULTS.diagnostic_event_limit.value,
        ge=20,
        le=10000,
        description="Maximum events in a redacted workflow diagnostic download.",
    )


__all__ = ["AccessWorkflowSettings", "MOVED_WORKFLOW_KEYS"]
