"""Trusted handler registry for administrator-installed permission definitions.

Installed definitions never carry executable code. They may reference only a
handler compiled into this runtime and described by the checked-in registry.
The loader requires both sides to agree exactly, which keeps an agent-authored
proposal inert until an operator/developer has supplied every trusted part.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from custom_messages.contracts.access_v2 import AccessLevel
from custom_messages.contracts.common_v1 import PermissionRisk, contract_sha256


# Access to a handler is the catalog tool procedure:<handler_id>, decided per
# call by the access service at the handler's ``level``; the descriptor digest
# pins every trusted field an installed definition relies on.
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALLED_PERMISSION_REGISTRY_PATH = (
    _REPO_ROOT / "ua_definitions" / "installed_permissions" / "handlers.yaml"
)


class InstalledPermissionHandlerDescriptorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = 1
    handler_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,159}$")
    display_name: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    input_schema: dict[str, Any]
    impact_description: str = Field(min_length=1, max_length=2000)
    risk: PermissionRisk
    # The access level invoking this procedure needs (the tool catalog lists it
    # as procedure:<handler_id>); set by trusted code review, never by agents.
    level: AccessLevel
    manager_delegable: bool

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != 1:
            raise ValueError(
                "only installed permission handler schema version 1 is supported"
            )
        return 1

    @field_validator("input_schema")
    @classmethod
    def _validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        schema = dict(value or {})
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError(
                "installed permission input schemas must describe an object"
            )
        if schema.get("additionalProperties") is not False:
            raise ValueError(
                "installed permission input schemas must fail closed on extra fields"
            )
        return schema

    @model_validator(mode="after")
    def _validate_delegable_risk(self) -> "InstalledPermissionHandlerDescriptorV1":
        if self.manager_delegable and self.risk not in {
            PermissionRisk.LOW,
            PermissionRisk.MEDIUM,
        }:
            raise ValueError(
                "manager-delegable installed permissions must be low or medium risk"
            )
        return self


class _InstalledPermissionRegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    handlers: dict[str, dict[str, Any]] = Field(default_factory=dict)


InstalledPermissionExecutor = Callable[[Any, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class TrustedInstalledPermissionHandler:
    descriptor: InstalledPermissionHandlerDescriptorV1
    descriptor_digest: str
    input_schema_digest: str
    executor: InstalledPermissionExecutor

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(arguments)
        Draft202012Validator(self.descriptor.input_schema).validate(normalized)
        return normalized


@dataclass(frozen=True)
class TrustedInstalledPermissionRegistry:
    handlers: Mapping[str, TrustedInstalledPermissionHandler]
    source_path: Path

    def get(self, handler_id: str) -> TrustedInstalledPermissionHandler | None:
        return self.handlers.get(str(handler_id or "").strip())

    def require(self, handler_id: str) -> TrustedInstalledPermissionHandler:
        normalized = str(handler_id or "").strip()
        handler = self.get(normalized)
        if handler is None:
            raise ValueError(
                f"Installed permission handler {normalized!r} is not registered in trusted runtime code and metadata."
            )
        return handler


# Runtime code registers callables here. The checked-in YAML must describe every
# entry and may not describe an entry without a corresponding callable.
TRUSTED_INSTALLED_PERMISSION_EXECUTORS: dict[str, InstalledPermissionExecutor] = {}


def load_trusted_installed_permission_registry(
    *,
    registry_path: str | Path | None = None,
    executors: Mapping[str, InstalledPermissionExecutor] | None = None,
) -> TrustedInstalledPermissionRegistry:
    path = (
        Path(registry_path or DEFAULT_INSTALLED_PERMISSION_REGISTRY_PATH)
        .expanduser()
        .resolve()
    )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(
            f"Unable to read installed permission registry {path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Invalid installed permission registry {path}: {exc}"
        ) from exc
    document = _InstalledPermissionRegistryDocument.model_validate(raw)
    if document.schema_version != 1:
        raise ValueError(
            "only installed permission registry schema version 1 is supported"
        )
    runtime_executors = dict(
        TRUSTED_INSTALLED_PERMISSION_EXECUTORS if executors is None else executors
    )
    if set(document.handlers) != set(runtime_executors):
        missing_code = sorted(set(document.handlers) - set(runtime_executors))
        missing_metadata = sorted(set(runtime_executors) - set(document.handlers))
        raise ValueError(
            "Installed permission handler code/metadata mismatch: "
            f"missing_code={missing_code}, missing_metadata={missing_metadata}"
        )
    handlers: dict[str, TrustedInstalledPermissionHandler] = {}
    for handler_id, payload in sorted(document.handlers.items()):
        descriptor = InstalledPermissionHandlerDescriptorV1.model_validate(
            {"handler_id": handler_id, **dict(payload)}
        )
        handlers[handler_id] = TrustedInstalledPermissionHandler(
            descriptor=descriptor,
            descriptor_digest=contract_sha256(descriptor),
            input_schema_digest=contract_sha256(descriptor.input_schema),
            executor=runtime_executors[handler_id],
        )
    return TrustedInstalledPermissionRegistry(
        handlers=MappingProxyType(handlers),
        source_path=path,
    )


__all__ = [
    "DEFAULT_INSTALLED_PERMISSION_REGISTRY_PATH",
    "InstalledPermissionExecutor",
    "InstalledPermissionHandlerDescriptorV1",
    "TRUSTED_INSTALLED_PERMISSION_EXECUTORS",
    "TrustedInstalledPermissionHandler",
    "TrustedInstalledPermissionRegistry",
    "load_trusted_installed_permission_registry",
]
