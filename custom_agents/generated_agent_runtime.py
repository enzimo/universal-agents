"""Startup check for reviewed generated-specialist definitions.

Provisioning plans and their approval live in ``generated_agent_provisioning``.
A generated specialist's bundle holds its definition (``agent.yaml``, which
extends ``TaskSpecialistTemplate`` and lists its default tools) and the record
of the approved change (``provisioning.json``). Before the agent is built this
module checks that the record still describes the definition: the same agent
type and team, the definition file unchanged since approval, and the same
default tools. Default tools grant nothing; every call is decided by the
access service (``custom_agents.access_runtime``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, ValidationError, field_validator, model_validator

from custom_agents.governance.authorized_operations import sha256_bytes
from custom_messages.contracts.access_v2 import ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import (
    ContractModel,
)
from custom_agents.team_composition import validate_agent_type_name


LOGGER = logging.getLogger(__name__)
# Version 3 lists the approved default tools (tool catalog ids) instead of
# capability registry bindings (access plan Phase 7). Older records belong to
# specialists whose definitions no longer load; they are re-created, not read.
GENERATED_AGENT_RECORD_SCHEMA_VERSION: Literal[3] = 3
MAX_GENERATED_DEFAULT_TOOLS = 8
GENERATED_AGENT_TEMPLATE_NAME: Literal["TaskSpecialistTemplate"] = (
    "TaskSpecialistTemplate"
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class GeneratedAgentProvisioningRecordV1(ContractModel):
    """Language-neutral activation/rollback record stored beside a generated bundle."""

    schema_version: Literal[3] = GENERATED_AGENT_RECORD_SCHEMA_VERSION
    record_type: Literal["generated_agent_provisioning"] = (
        "generated_agent_provisioning"
    )
    provisioning_id: str = Field(min_length=1, max_length=240)
    change_id: str = Field(min_length=1, max_length=240)
    plan_digest: str = Field(min_length=64, max_length=64)
    status: Literal["ready", "rolled_back"]
    agent_type: str = Field(min_length=3, max_length=64)
    template_name: Literal["TaskSpecialistTemplate"] = GENERATED_AGENT_TEMPLATE_NAME
    team_id: str = Field(min_length=1, max_length=240)
    owner_canonical_user_id: str = Field(min_length=1, max_length=240)
    actor_principal_id: str = Field(min_length=1, max_length=240)
    # The tool catalog ids or tool set ids the person approved, sorted.
    default_tools: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_GENERATED_DEFAULT_TOOLS,
    )
    catalog_sha256: str = Field(min_length=64, max_length=64)
    # How the specialist was approved (access_control.changes); records in the
    # older decision-id format are refused at startup (approve-existing).
    approval: ChangeApprovalRecordV1
    created_at_utc: datetime
    applied_at_utc: datetime
    rolled_back_at_utc: datetime | None = None

    @field_validator("plan_digest", "catalog_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("provisioning digests must be lowercase SHA-256 values")
        return normalized

    @field_validator("agent_type")
    @classmethod
    def _validate_agent_type(cls, value: str) -> str:
        return validate_agent_type_name(value)

    @field_validator("created_at_utc", "applied_at_utc", "rolled_back_at_utc")
    @classmethod
    def _normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provisioning timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_provenance(self) -> GeneratedAgentProvisioningRecordV1:
        if self.plan_digest == self.catalog_sha256:
            raise ValueError(
                "plan and catalog digests must describe distinct artifacts"
            )
        if list(self.default_tools) != sorted(set(self.default_tools)):
            raise ValueError("generated default tools must be unique and sorted")
        if self.status == "ready" and self.rolled_back_at_utc is not None:
            raise ValueError("ready provisioning cannot have a rollback timestamp")
        if self.status == "rolled_back" and self.rolled_back_at_utc is None:
            raise ValueError("rolled-back provisioning requires a rollback timestamp")
        return self


def _configured_default_tools(agent_config: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(item or "").strip()
                for item in agent_config.get("default_tools", []) or []
                if str(item or "").strip()
            }
        )
    )


class GeneratedAgentRuntimeHydrator:
    """Validate one generated definition against its approved record."""

    def __init__(self, agent_config: dict[str, Any]) -> None:
        self.agent_config = agent_config

    def apply(self) -> dict[str, Any]:
        record_path_raw = str(
            self.agent_config.get("provisioning_record_path") or ""
        ).strip()
        if not record_path_raw:
            return self.agent_config
        default_tools = _configured_default_tools(self.agent_config)
        if not default_tools:
            raise ValueError("Provisioned agent definition omitted default_tools.")

        record = self._load_and_validate_record(
            Path(record_path_raw).expanduser().resolve(),
            default_tools=default_tools,
        )
        LOGGER.info(
            "Validated generated-agent provisioning. "
            "event=generated_agent_provisioning_validated agent_type=%s team_id=%s default_tools=%s",
            record.agent_type,
            record.team_id,
            list(default_tools),
        )
        return self.agent_config

    def _load_and_validate_record(
        self,
        record_path: Path,
        *,
        default_tools: tuple[str, ...],
    ) -> GeneratedAgentProvisioningRecordV1:
        try:
            record = GeneratedAgentProvisioningRecordV1.model_validate_json(
                record_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ValueError(
                f"Generated-agent provisioning record is unavailable or invalid: {exc}"
            ) from exc
        if record.status != "ready":
            raise ValueError("Generated-agent provisioning record is not ready.")
        if (
            record.agent_type
            != str(self.agent_config.get("named_agent_type") or "").strip()
        ):
            raise ValueError(
                "Generated-agent provisioning record names another agent type."
            )
        if record.team_id != str(self.agent_config.get("team_id") or "").strip():
            raise ValueError(
                "Generated-agent provisioning record belongs to another team."
            )
        catalog_path = (
            Path(str(self.agent_config.get("agent_definition_source_path") or ""))
            .expanduser()
            .resolve()
        )
        if (
            not catalog_path.is_file()
            or sha256_bytes(catalog_path.read_bytes()) != record.catalog_sha256
        ):
            raise ValueError(
                "Generated-agent catalog changed after reviewed provisioning."
            )
        if record.default_tools != default_tools:
            raise ValueError(
                "Generated-agent default tools do not match the approved record."
            )
        return record


def apply_generated_agent_provisioning(
    agent_config: dict[str, Any],
) -> dict[str, Any]:
    """Apply reviewed generated-definition provenance to runtime configuration."""

    return GeneratedAgentRuntimeHydrator(agent_config).apply()
