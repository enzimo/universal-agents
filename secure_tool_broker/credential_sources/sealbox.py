"""Sealbox credential source adapter for broker-owned automations."""

from __future__ import annotations

import os
from typing import Any, Mapping

from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.credential_state import active_credential_owner_id
from secure_tool_broker.credential_sources.base import (
    CredentialMetadata,
    CredentialRef,
    CredentialSearchRequest,
    CredentialSecretRequest,
    GITHUB_AUTH_AUTOMATION,
    SecretFieldMetadata,
)
from secure_tool_broker.sealbox_cli import (
    auth_required_result,
    first_json_value,
    is_auth_required,
    redacted_completed_content,
    reject_unsupported_key,
    run_sealbox,
)
from secure_tool_broker.sealbox_tenants import SEALBOX_TENANT_NOT_PROVISIONED, sealbox_tenant_environment
from secure_tools.providers import ToolResult


def _string(value: Any) -> str:
    return str(value or "").strip()


def _item_reference(ref: CredentialRef) -> str:
    return _string(ref.item_id or ref.item_name)


def _configured() -> bool:
    return bool(os.getenv("SEALBOX_URL") and os.getenv("SEALBOX_ROOT_TOKEN_FILE"))


def _credential_metadata_from_item(item: Mapping[str, Any]) -> CredentialMetadata:
    key = _string(item.get("key"))
    username = _string(item.get("username"))
    secret_fields = (
        SecretFieldMetadata(
            field_ref="password",
            label="password",
            kind="password",
            secret_value_present=True,
        ),
    )
    return CredentialMetadata(
        credential_ref=CredentialRef(source_provider="sealbox", item_id=key or None, item_name=key or None),
        display_name=key,
        username=username or None,
        username_present=bool(username),
        urls=(),
        domains=(),
        vault=None,
        folder=None,
        tags=(),
        item_type="credential",
        revision_date=None,
        secret_fields=secret_fields,
        supported_automations=(GITHUB_AUTH_AUTOMATION.to_dict(),),
    )


class SealboxCredentialSource:
    name = "sealbox"

    def auth_status(self) -> AuthStatus:
        configured = _configured()
        owner_id = active_credential_owner_id()
        tenant_env = None
        if owner_id:
            tenant_env = sealbox_tenant_environment(
                scope_kind="user",
                owner_id=owner_id,
                provision=False,
            )
            if isinstance(tenant_env, ToolResult):
                if tenant_env.content.get("error_code") == "sealbox_tenant_not_provisioned":
                    return AuthStatus(
                        provider="sealbox",
                        status="configured" if configured else "auth_required",
                        authenticated=False,
                        message="Sealbox is configured; this user has no tenant until their first credential write.",
                        auth_options=("managed_service", "external_service"),
                    )
                return AuthStatus(
                    provider="sealbox",
                    status="unavailable",
                    authenticated=False,
                    message=tenant_env.error or "Sealbox tenant material is unavailable.",
                    auth_options=("managed_service", "external_service"),
                    details=tenant_env.content,
                )
        completed = run_sealbox(
            ["key", "status", "--output", "json"],
            env=tenant_env,
            timeout_seconds=15,
        )
        if isinstance(completed, ToolResult):
            return AuthStatus(
                provider="sealbox",
                status="unavailable",
                authenticated=False,
                message=completed.error or "Sealbox CLI is unavailable.",
                auth_options=("managed_service", "env", "token_file", "key_files"),
                details=completed.content,
            )
        parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, Mapping))
        content = redacted_completed_content(
            completed,
            action="auth.status",
            command=["sealbox-cli", "key", "status", "--output", "json"],
        )
        if not isinstance(parsed, Mapping):
            return AuthStatus(
                provider="sealbox",
                status="configured" if configured else "auth_required",
                authenticated=False,
                message="Sealbox key status did not return JSON metadata.",
                auth_options=("managed_service", "env", "token_file", "key_files"),
                details=content,
            )
        local_keys = parsed.get("local_keys") if isinstance(parsed.get("local_keys"), Mapping) else {}
        server_keys = parsed.get("server_keys") if isinstance(parsed.get("server_keys"), Mapping) else {}
        local_valid = bool(local_keys.get("key_pair_valid"))
        server_count = int(server_keys.get("count") or 0) if str(server_keys.get("count") or "").isdigit() else 0
        server_error = _string(server_keys.get("error"))
        content["json"] = parsed
        if completed.returncode == 0 and local_valid and server_count > 0:
            return AuthStatus(
                provider="sealbox",
                status="authenticated",
                authenticated=True,
                message="This user's Sealbox tenant token and key pair are configured.",
                auth_options=("managed_service", "env", "token_file", "key_files"),
                details=content,
            )
        if server_error and is_auth_required(error=server_error):
            return AuthStatus(
                provider="sealbox",
                status="auth_required",
                authenticated=False,
                message="Sealbox authentication token is missing or rejected.",
                auth_options=("managed_service", "env", "token_file", "key_files"),
                details=content,
            )
        return AuthStatus(
            provider="sealbox",
            status="configured" if configured or local_valid else "auth_required",
            authenticated=False,
            message=(
                "Sealbox is configured but no server master key is available."
                if local_valid
                else "Sealbox authentication is required. Configure token and key files for the broker."
            ),
            auth_options=("managed_service", "env", "token_file", "key_files"),
            details=content,
        )

    def _list_credentials(
        self,
        request: CredentialSearchRequest | None = None,
        *,
        owner_scope_kind: str | None = None,
        owner_id: str | None = None,
    ) -> list[Mapping[str, Any]] | ToolResult:
        command = ["credential", "list", "--output", "json"]
        if request is not None:
            if request.query.strip():
                command.extend(["--query", request.query.strip()])
            key = _string(request.key)
            name = _string(request.name)
            username = _string(request.username)
            if key:
                command.extend(["--key", key])
            if name:
                command.extend(["--name", name])
            if username:
                command.extend(["--username", username])
        resolved_scope_kind = request.owner_scope_kind if request is not None else owner_scope_kind
        resolved_owner_id = request.owner_id if request is not None else owner_id
        tenant_env = None
        if resolved_owner_id:
            tenant_env = sealbox_tenant_environment(
                scope_kind=resolved_scope_kind,
                owner_id=resolved_owner_id,
                provision=False,
            )
            if isinstance(tenant_env, ToolResult):
                return tenant_env
        completed = run_sealbox(command, env=tenant_env, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        content = redacted_completed_content(
            completed,
            action="credential.list",
            command=["sealbox-cli", *command],
        )
        if completed.returncode != 0:
            if is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return auth_required_result(content)
            return ToolResult(status="error", content=content, error=f"sealbox-cli exited with code {completed.returncode}")
        parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, list))
        if parsed is None:
            return []
        return [item for item in parsed if isinstance(item, Mapping)]

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        listed = self._list_credentials(request)
        if isinstance(listed, ToolResult):
            if listed.content.get("error_code") == SEALBOX_TENANT_NOT_PROVISIONED:
                return ToolResult(
                    status="success",
                    content={
                        "provider": "sealbox",
                        "action": "credential.search",
                        "query": request.query,
                        "count": 0,
                        "returned_count": 0,
                        "credentials": [],
                    },
                )
            return listed
        credentials = []
        for item in listed:
            metadata = _credential_metadata_from_item(item).to_dict()
            credentials.append(metadata)
        bounded_limit = max(1, min(int(request.limit), 100))
        return ToolResult(
            status="success",
            content={
                "provider": "sealbox",
                "action": "credential.search",
                "query": request.query,
                "key": request.key,
                "name": request.name,
                "username": request.username,
                "count": len(credentials),
                "returned_count": min(len(credentials), bounded_limit),
                "credentials": credentials[:bounded_limit],
            },
        )

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        key = _item_reference(ref)
        if not key:
            return ToolResult(status="error", error="Sealbox item_id or item_name is required.")
        invalid = reject_unsupported_key(key)
        if invalid is not None:
            return invalid
        listed = self._list_credentials(
            owner_scope_kind=ref.owner_scope_kind,
            owner_id=ref.owner_id,
        )
        if isinstance(listed, ToolResult):
            return listed
        for item in listed:
            if _string(item.get("key")) == key:
                return ToolResult(
                    status="success",
                    content={
                        "provider": "sealbox",
                        "action": "credential.get_metadata",
                        "credential": _credential_metadata_from_item(item).to_dict(),
                    },
                )
        return ToolResult(
            status="error",
            error=f"Sealbox credential metadata not found: {key}",
            content={"error_code": "credential_not_found", "provider": "sealbox", "item_id": key},
        )

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        key = _item_reference(request.ref)
        if not key:
            return ToolResult(status="error", error="Sealbox item_id or item_name is required.")
        invalid = reject_unsupported_key(key)
        if invalid is not None:
            return invalid
        field_ref = _string(request.field_ref or "password")
        if field_ref in {"", "password"}:
            command = ["credential", "get", key, "--output", "json"]
            value_key = "password"
            action = "credential.get"
        elif field_ref == "value":
            command = ["secret", "get", key, "--output", "json"]
            value_key = "value"
            action = "secret.get"
        else:
            command = ["secret", "get", f"{key}:{field_ref}", "--output", "json"]
            value_key = "value"
            action = "secret.get"
        resolved_owner_id = request.owner_id or request.ref.owner_id
        tenant_env = None
        if resolved_owner_id:
            tenant_env = sealbox_tenant_environment(
                scope_kind=request.owner_scope_kind or request.ref.owner_scope_kind,
                owner_id=resolved_owner_id,
                provision=False,
            )
            if isinstance(tenant_env, ToolResult):
                return tenant_env
        completed = run_sealbox(command, env=tenant_env, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        content = redacted_completed_content(
            completed,
            action=action,
            command=["sealbox-cli", *command],
            include_stdout=False,
        )
        if completed.returncode != 0:
            if is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return auth_required_result(content)
            error_output = (completed.stdout + completed.stderr).lower()
            if "[sealboxerror] secret not found:" in error_output:
                return ToolResult(
                    status="error", content={"error_code": "credential_not_found"},
                    error="The requested Sealbox secret does not exist.",
                )
            return ToolResult(status="error", content=content, error=f"sealbox-cli exited with code {completed.returncode}")
        parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, Mapping))
        if not isinstance(parsed, Mapping):
            return ToolResult(status="error", content=content, error="sealbox-cli did not return JSON secret output.")
        secret = _string(parsed.get(value_key))
        if not secret:
            return ToolResult(status="error", error=f"Sealbox output did not include a non-empty {value_key!r} field.")
        return secret
