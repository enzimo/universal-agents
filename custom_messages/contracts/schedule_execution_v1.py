"""Leaf authorization condition shared by schedule and permission contracts."""

from pydantic import BaseModel, ConfigDict, Field


class ScheduleExecutionScopeV1(BaseModel):
    """Verified schedule identity and behavioral revision, never a bearer grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schedule_id: str = Field(min_length=1, max_length=240)
    execution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
