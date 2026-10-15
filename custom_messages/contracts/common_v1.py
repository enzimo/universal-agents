"""Shared version 1 contract pieces: the frozen model base, digests, risk and principals.

These are what the capability, MCP, installed-procedure, generated-agent and
execution contracts share. They lived in ``authorization_v1`` until the old
permission system that module described was removed (access Phase 4d); access
itself is now described by ``access_v2``.

- ``ContractModel`` is the base for immutable, strictly validated records.
- ``canonical_contract_json`` and ``contract_sha256`` give the one canonical
  JSON form (sorted keys, no whitespace, ``None`` fields left out) and its
  SHA-256, used for plan, record and descriptor digests.
- ``PermissionRisk`` is the risk a capability or procedure declares.
- ``PrincipalDescriptorV1`` names who a broker request acts for (MCP calls).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import TypeAliasType

# JSON a contract attribute may hold. Floats are left out on purpose: their
# text form differs between languages, which would change digests.
ContractJsonValue = TypeAliasType(
    "ContractJsonValue",
    Union[
        None,
        bool,
        int,
        str,
        list["ContractJsonValue"],
        dict[str, "ContractJsonValue"],
    ],
)
_MAX_ATTRIBUTE_DEPTH = 12


class ContractModel(BaseModel):
    """Immutable record that refuses unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PrincipalType(StrEnum):
    USER = "User"
    AGENT = "Agent"
    SERVICE = "Service"
    SYSTEM = "System"


class PermissionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_contract_json(value: BaseModel | Mapping[str, Any]) -> str:
    """The canonical JSON text of a record, as hashed by ``contract_sha256``."""

    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=True)
    else:
        payload = dict(value)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_sha256(value: BaseModel | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_contract_json(value).encode("utf-8")).hexdigest()


def _normalize_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic JSON data; floats are refused (see ``ContractJsonValue``)."""

    def normalize(item: Any, *, depth: int) -> Any:
        if depth > _MAX_ATTRIBUTE_DEPTH:
            raise ValueError(f"contract attributes cannot exceed {_MAX_ATTRIBUTE_DEPTH} levels of nesting")
        if item is None or isinstance(item, (str, bool)):
            return item
        if isinstance(item, int):
            if item < -(2**63) or item > 2**63 - 1:
                raise ValueError("contract attribute integers must fit the signed 64-bit range")
            return item
        if isinstance(item, float):
            raise ValueError("contract attributes do not support floating-point values")
        if isinstance(item, Mapping):
            normalized: dict[str, Any] = {}
            for key, nested in item.items():
                if not isinstance(key, str) or not key:
                    raise ValueError("contract attribute object keys must be non-empty strings")
                normalized[key] = normalize(nested, depth=depth + 1)
            return normalized
        if isinstance(item, (list, tuple)):
            return [normalize(nested, depth=depth + 1) for nested in item]
        raise ValueError(f"contract attributes contain unsupported type {type(item).__name__}")

    return normalize(dict(value), depth=0)


class PrincipalDescriptorV1(ContractModel):
    """Who a request acts as, and for which person (``on_behalf_of_user_id``)."""

    principal_type: PrincipalType
    principal_id: str = Field(min_length=1, max_length=240)
    team_id: Optional[str] = Field(default=None, max_length=240)
    named_agent_type: Optional[str] = Field(default=None, max_length=240)
    on_behalf_of_user_id: Optional[str] = Field(default=None, max_length=240)
    roles: tuple[str, ...] = ()
    attributes: dict[str, ContractJsonValue] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def _normalize_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @field_validator("attributes", mode="before")
    @classmethod
    def _normalize_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_json_mapping(value)


__all__ = [
    "ContractJsonValue",
    "ContractModel",
    "PermissionRisk",
    "PrincipalDescriptorV1",
    "PrincipalType",
    "canonical_contract_json",
    "contract_sha256",
    "utc_now",
]
