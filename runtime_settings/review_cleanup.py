"""Operator-owned bounds for retiring abandoned memory-review claims."""

from pydantic import BaseModel, ConfigDict, Field


class ReviewCleanupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    scan_interval_seconds: int = Field(default=300, ge=10)
    expired_claim_grace_seconds: int = Field(default=3600, ge=0)
    batch_size: int = Field(default=100, ge=1, le=1000)
    archive_retention_days: int = Field(default=90, ge=1)
