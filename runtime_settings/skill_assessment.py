"""Non-secret operator settings for optional skill semantic assessment."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from default_values import WORKSPACE_DEFAULTS

# The most a review may send to a model (1 MiB); refusal hints name it too.
MAX_INPUT_BYTES_CEILING = 1048576


class SkillSemanticReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "task_model"] = WORKSPACE_DEFAULTS.skill_installation.value[
        "semantic_review"
    ]["mode"]
    max_input_bytes: int = Field(
        default=WORKSPACE_DEFAULTS.skill_installation.value["semantic_review"]["max_input_bytes"],
        ge=1024,
        le=MAX_INPUT_BYTES_CEILING,
    )
