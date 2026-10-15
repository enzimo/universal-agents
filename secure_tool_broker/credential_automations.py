"""Broker-owned automations that consume credential-source secrets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from secure_tool_broker.credential_sources import (
    CredentialRef,
    CredentialSecretRequest,
    get_credential_source,
    normalize_credential_source_name,
)
from secure_tools.providers import ToolContext, ToolResult


@dataclass(frozen=True)
class CredentialAutomation:
    target_provider: str
    target_action: str
    description: str
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_provider": self.target_provider,
            "target_action": self.target_action,
            "description": self.description,
            "required_params": list(self.required_params),
            "optional_params": list(self.optional_params),
        }


SUPPORTED_CREDENTIAL_AUTOMATIONS = (
    CredentialAutomation(
        target_provider="github",
        target_action="auth.login",
        description="Log the broker-owned gh CLI in using a token from a credential source.",
        required_params=("credential_provider", "item_id or item_name"),
        optional_params=("field_ref", "hostname", "vault", "folder"),
    ),
)


class GitHubTokenLogin(Protocol):
    """Narrow executor port used by credential-backed GitHub login."""

    def __call__(
        self, token: str, *, hostname: str | None = None
    ) -> ToolResult: ...


def supported_automation_dicts() -> list[dict[str, Any]]:
    return [automation.to_dict() for automation in SUPPORTED_CREDENTIAL_AUTOMATIONS]


def _normalize_target_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return "github" if provider == "gh" else provider


def _normalize_target_action(value: str) -> str:
    action = str(value or "").strip().lower()
    if action in {"login", "github.auth.login", "gh.auth.login", "auth.login.bitwarden", "auth.login.credential"}:
        return "auth.login"
    return action


def _unsupported_target_result(target_provider: str, target_action: str) -> ToolResult:
    return ToolResult(
        status="error",
        content={
            "target_provider": target_provider,
            "target_action": target_action,
            "supported_automations": supported_automation_dicts(),
        },
        error="Unsupported credential automation target.",
    )


def use_credential_for_action(
    *,
    credential_provider: str,
    target_provider: str,
    target_action: str,
    item_id: str | None,
    item_name: str | None,
    field_ref: str = "password",
    hostname: str | None = None,
    vault: str | None = None,
    folder: str | None = None,
    context: ToolContext | None = None,
    github_token_login: GitHubTokenLogin,
) -> ToolResult:
    source_provider = normalize_credential_source_name(credential_provider)
    if not source_provider:
        return ToolResult(status="error", error="credential_provider is required.")
    normalized_target_provider = _normalize_target_provider(target_provider)
    if not normalized_target_provider:
        return ToolResult(status="error", error="target_provider is required.")
    normalized_target_action = _normalize_target_action(target_action)
    if not normalized_target_action:
        return ToolResult(status="error", error="target_action is required.")
    if not (item_id or item_name):
        return ToolResult(status="error", error="item_id or item_name is required.")

    source = get_credential_source(source_provider)
    if source is None:
        return ToolResult(
            status="error",
            content={"credential_provider": source_provider},
            error=f"Unsupported credential provider: {source_provider}",
        )

    if normalized_target_provider != "github" or normalized_target_action != "auth.login":
        return _unsupported_target_result(normalized_target_provider, normalized_target_action)

    owner_id = str(context.canonical_user_id or "").strip() if context is not None else ""
    secret = source.get_secret(
        CredentialSecretRequest(
            ref=CredentialRef(
                source_provider=source_provider,
                item_id=item_id,
                item_name=item_name,
                vault=vault,
                folder=folder,
                owner_scope_kind="user" if owner_id else None,
                owner_id=owner_id or None,
            ),
            field_ref=field_ref or "password",
            owner_scope_kind="user" if owner_id else None,
            owner_id=owner_id or None,
        )
    )
    if isinstance(secret, ToolResult):
        return secret

    result = github_token_login(secret, hostname=hostname or "github.com")
    content = dict(result.content)
    credential_ref = {
        "source_provider": source_provider,
        "item_id": item_id or "",
        "item_name": item_name or "",
    }
    if vault:
        credential_ref["vault"] = vault
    if folder:
        credential_ref["folder"] = folder
    content.update(
        {
            "credential_source": source_provider,
            "credential_provider": source_provider,
            "credential_ref": credential_ref,
            "credential_field_ref": field_ref or "password",
            "target_provider": normalized_target_provider,
            "target_action": normalized_target_action,
        }
    )
    if source_provider == "bitwarden":
        content.update(
            {
                "bitwarden_item_id": item_id or "",
                "bitwarden_item_name": item_name or "",
                "bitwarden_field": field_ref or "password",
            }
        )
    result.content = content
    return result
