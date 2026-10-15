"""Provider-neutral credential source contracts for secure broker automations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from secure_tool_broker.auth_models import AuthStatus
from secure_tools.providers import ToolResult


@dataclass(frozen=True)
class CredentialRef:
    source_provider: str
    item_id: str | None = None
    item_name: str | None = None
    vault: str | None = None
    folder: str | None = None
    owner_scope_kind: str | None = None
    owner_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_provider": self.source_provider,
            "item_id": self.item_id,
            "item_name": self.item_name,
        }


@dataclass(frozen=True)
class SecretFieldMetadata:
    field_ref: str
    label: str
    kind: str
    secret_value_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_ref": self.field_ref,
            "label": self.label,
            "kind": self.kind,
            "secret_value_present": self.secret_value_present,
        }


@dataclass(frozen=True)
class CredentialMetadata:
    credential_ref: CredentialRef
    display_name: str
    username: str | None
    username_present: bool
    urls: tuple[str, ...]
    domains: tuple[str, ...]
    vault: str | None
    folder: str | None
    tags: tuple[str, ...]
    item_type: str | None
    revision_date: str | None
    secret_fields: tuple[SecretFieldMetadata, ...]
    supported_automations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_ref": self.credential_ref.to_dict(),
            "display_name": self.display_name,
            "username": self.username,
            "username_present": self.username_present,
            "urls": list(self.urls),
            "domains": list(self.domains),
            "vault": self.vault,
            "folder": self.folder,
            "tags": list(self.tags),
            "item_type": self.item_type,
            "revision_date": self.revision_date,
            "secret_fields": [field.to_dict() for field in self.secret_fields],
            "supported_automations": [dict(automation) for automation in self.supported_automations],
        }


@dataclass(frozen=True)
class CredentialSearchRequest:
    query: str = ""
    limit: int = 50
    vault: str | None = None
    folder: str | None = None
    key: str | None = None
    name: str | None = None
    username: str | None = None
    include_archived: bool = False
    owner_scope_kind: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True)
class CredentialSecretRequest:
    ref: CredentialRef
    field_ref: str = "password"
    owner_scope_kind: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True)
class CredentialAutomationMetadata:
    target_provider: str
    target_action: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "target_provider": self.target_provider,
            "target_action": self.target_action,
            "description": self.description,
        }


GITHUB_AUTH_AUTOMATION = CredentialAutomationMetadata(
    target_provider="github",
    target_action="auth.login",
    description="Log the broker-owned gh CLI in using this credential.",
)


class CredentialSource(Protocol):
    name: str

    def auth_status(self) -> AuthStatus:
        ...

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        ...

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        ...

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        ...


_CREDENTIAL_SOURCES: dict[str, CredentialSource] = {}
_CREDENTIAL_SOURCE_ALIASES: dict[str, str] = {
    "bw": "bitwarden",
    "1password": "onepassword",
    "1password-cli": "onepassword",
    "op": "onepassword",
    "keepassxc": "keepass",
    "keepassxc-cli": "keepass",
    "hashicorp-vault": "vault",
    "sealbox-cli": "sealbox",
}


def normalize_credential_source_name(name: str) -> str:
    source_name = str(name or "").strip().lower()
    return _CREDENTIAL_SOURCE_ALIASES.get(source_name, source_name)


def register_credential_source(source: CredentialSource) -> None:
    _CREDENTIAL_SOURCES[normalize_credential_source_name(source.name)] = source


def get_credential_source(name: str) -> CredentialSource | None:
    return _CREDENTIAL_SOURCES.get(normalize_credential_source_name(name))


def list_credential_sources() -> list[str]:
    return sorted(_CREDENTIAL_SOURCES)
