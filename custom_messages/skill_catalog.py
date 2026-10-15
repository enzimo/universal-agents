"""Versioned messages for managed skill catalog convergence."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from id_generation import new_id


class ManagedSkillCatalogChangedV1(BaseModel):
    """Authority-free hint that a trusted managed-skill receipt was committed.

    The receiver must verify the referenced immutable receipt from its own
    instance storage before refreshing. The message never carries a path,
    instructions, executable content, or permission.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    message_class: Literal["ManagedSkillCatalogChangedV1"] = (
        "ManagedSkillCatalogChangedV1"
    )
    notification_id: str = Field(
        default_factory=lambda: new_id("skill_cat_msg"),
        min_length=1,
        max_length=240,
    )
    team_id: str = Field(min_length=1, max_length=240)
    source_agent_id: str = Field(min_length=1, max_length=240)
    source_named_agent_type: Literal["TeamArchitect"] = "TeamArchitect"
    operation: Literal["install", "uninstall"]
    change_id: str = Field(min_length=1, max_length=240)
    skill_name: str = Field(min_length=1, max_length=240)
    installed_tree_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("installed_tree_sha256", mode="before")
    @classmethod
    def _normalize_digest(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("created_at_utc")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at_utc must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_mutation_identity(self) -> "ManagedSkillCatalogChangedV1":
        expected_prefix = f"skill_{self.operation}_"
        if not re.fullmatch(rf"{expected_prefix}[0-9a-f]{{12}}", self.change_id):
            raise ValueError(
                f"{self.operation} notifications require a valid {expected_prefix} change id"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", self.skill_name):
            raise ValueError("skill_name must be one direct managed skill name")
        return self

    def get_id(self) -> str:
        return self.notification_id


__all__ = ["ManagedSkillCatalogChangedV1"]
