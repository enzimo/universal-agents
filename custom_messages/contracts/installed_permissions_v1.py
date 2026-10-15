"""Versioned contracts for proposed and installed managed permissions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from custom_messages.contracts.common_v1 import PermissionRisk, contract_sha256


INSTALLED_PERMISSION_SCHEMA_VERSION = 1
_PROPOSAL_ID_PATTERN = re.compile(r"perm_cap_prop_[0-9a-f]{12}\Z")
_DEFINITION_ID_PATTERN = re.compile(
    r"installed_permission\.[a-z][a-z0-9_.-]{2,159}\.[0-9a-f]{16}\Z"
)


def normalize_permission_proposal_id(value: str) -> str:
    """Validate and normalize an installed-permission proposal identifier."""

    normalized = str(value or "").strip()
    if not _PROPOSAL_ID_PATTERN.fullmatch(normalized):
        raise ValueError("invalid installed permission proposal id")
    return normalized


def normalize_installed_permission_definition_id(value: str) -> str:
    """Validate and normalize an installed-permission definition identifier."""

    normalized = str(value or "").strip()
    if not _DEFINITION_ID_PATTERN.fullmatch(normalized):
        raise ValueError("invalid installed permission definition id")
    return normalized


class PermissionCapabilityProposalStatus(StrEnum):
    PENDING_REGISTRATION = "pending_registration"
    READY_FOR_ADMIN = "ready_for_admin"
    INSTALLED = "installed"
    REJECTED = "rejected"


class PermissionCapabilityProposalV1(BaseModel):
    """Immutable-at-the-boundary description of a requested managed operation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = INSTALLED_PERMISSION_SCHEMA_VERSION
    proposal_id: str
    proposal_digest: str = ""
    created_at_utc: datetime
    updated_at_utc: datetime
    proposer_principal_id: str = Field(min_length=1, max_length=240)
    proposer_named_agent_type: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    owner_canonical_user_id: str = Field(min_length=1, max_length=240)
    source_task_id: str = Field(min_length=1, max_length=240)
    requested_handler_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    display_name: str = Field(min_length=1, max_length=240)
    purpose: str = Field(min_length=1, max_length=2000)
    justification: str = Field(min_length=1, max_length=4000)
    status: PermissionCapabilityProposalStatus
    resolved_at_utc: datetime | None = None
    resolved_by_principal_id: str | None = Field(default=None, max_length=240)
    resolution_reason: str | None = Field(default=None, max_length=2000)
    installed_definition_id: str | None = Field(default=None, max_length=240)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != INSTALLED_PERMISSION_SCHEMA_VERSION:
            raise ValueError(
                "only installed permission proposal schema version 1 is supported"
            )
        return int(value)

    @field_validator("proposal_id")
    @classmethod
    def _validate_proposal_id(cls, value: str) -> str:
        return normalize_permission_proposal_id(value)

    @model_validator(mode="after")
    def _validate_digest_and_state(self) -> "PermissionCapabilityProposalV1":
        if self.proposal_digest and self.proposal_digest != self.calculate_digest():
            raise ValueError("installed permission proposal digest mismatch")
        terminal = self.status in {
            PermissionCapabilityProposalStatus.INSTALLED,
            PermissionCapabilityProposalStatus.REJECTED,
        }
        if terminal != bool(self.resolved_at_utc and self.resolved_by_principal_id):
            raise ValueError(
                "installed permission proposal resolution state is incomplete"
            )
        if self.status == PermissionCapabilityProposalStatus.INSTALLED:
            if not self.installed_definition_id:
                raise ValueError("installed proposal must reference its definition")
        elif self.installed_definition_id is not None:
            raise ValueError("only installed proposals may reference a definition")
        return self

    def calculate_digest(self) -> str:
        return contract_sha256(
            self.model_dump(
                mode="json",
                exclude={
                    "proposal_digest",
                    "updated_at_utc",
                    "status",
                    "resolved_at_utc",
                    "resolved_by_principal_id",
                    "resolution_reason",
                    "installed_definition_id",
                },
                exclude_none=True,
            )
        )

    @classmethod
    def build(cls, values: Mapping[str, object]) -> "PermissionCapabilityProposalV1":
        payload = dict(values)
        payload["proposal_digest"] = ""
        candidate = cls.model_validate(payload)
        return cls.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "proposal_digest": candidate.calculate_digest(),
            }
        )


_PROPOSAL_VIEW_FIELDS = frozenset({"handler_registered", "administrator_installable", "trusted_handler"})


class TrustedHandlerViewV1(BaseModel):
    """What administrators see of a registered handler before installing it."""

    model_config = ConfigDict(extra="forbid")

    handler_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    impact_description: str = Field(min_length=1, max_length=2000)
    risk: PermissionRisk
    level: str = Field(min_length=1, max_length=40)  # the access level calling it needs
    handler_descriptor_digest: str = Field(min_length=64, max_length=64)
    input_schema_digest: str = Field(min_length=64, max_length=64)


class PermissionCapabilityProposalViewV1(PermissionCapabilityProposalV1):
    """A proposal as administrators see it: its current status and its handler, if registered."""

    handler_registered: bool
    administrator_installable: bool
    trusted_handler: TrustedHandlerViewV1 | None = None

    def calculate_digest(self) -> str:
        # The digest is the stored proposal's; the view fields are not part of it.
        stored = PermissionCapabilityProposalV1.model_construct(
            **self.model_dump(exclude=set(_PROPOSAL_VIEW_FIELDS))
        )
        return stored.calculate_digest()


class InstalledPermissionDefinitionV1(BaseModel):
    """Installed, content-addressed definition of a trusted managed operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = INSTALLED_PERMISSION_SCHEMA_VERSION
    definition_id: str
    definition_digest: str = ""
    proposal_id: str
    installed_at_utc: datetime
    installed_by_principal_id: str
    team_id: str
    owner_canonical_user_id: str
    handler_id: str
    handler_descriptor_digest: str
    input_schema_digest: str
    display_name: str
    description: str
    impact_description: str
    risk: PermissionRisk
    manager_delegable: bool

    @field_validator("definition_id")
    @classmethod
    def _validate_definition_id(cls, value: str) -> str:
        return normalize_installed_permission_definition_id(value)

    @field_validator(
        "definition_digest",
        "handler_descriptor_digest",
        "input_schema_digest",
    )
    @classmethod
    def _validate_digest(cls, value: str, info: ValidationInfo) -> str:
        if info.field_name == "definition_digest" and value == "":
            return value
        normalized = str(value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def _validate_definition_digest(self) -> "InstalledPermissionDefinitionV1":
        if self.definition_digest and self.definition_digest != self.calculate_digest():
            raise ValueError("installed permission definition digest mismatch")
        return self

    def calculate_digest(self) -> str:
        return contract_sha256(
            self.model_dump(mode="json", exclude={"definition_digest"})
        )

    @classmethod
    def build(cls, values: Mapping[str, object]) -> "InstalledPermissionDefinitionV1":
        payload = dict(values)
        payload["definition_digest"] = ""
        candidate = cls.model_validate(payload)
        return cls.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "definition_digest": candidate.calculate_digest(),
            }
        )


__all__ = [
    "INSTALLED_PERMISSION_SCHEMA_VERSION",
    "InstalledPermissionDefinitionV1",
    "PermissionCapabilityProposalStatus",
    "PermissionCapabilityProposalV1",
    "PermissionCapabilityProposalViewV1",
    "TrustedHandlerViewV1",
    "normalize_installed_permission_definition_id",
    "normalize_permission_proposal_id",
]
