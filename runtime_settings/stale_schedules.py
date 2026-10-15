"""Operator-owned bounds for the stale-schedule check (``custom_agents.stale_schedules``)."""

from pydantic import BaseModel, ConfigDict, Field


class StaleScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    turned_off_days: int = Field(default=30, ge=1)
    paused_beyond_days: int = Field(default=90, ge=1)
