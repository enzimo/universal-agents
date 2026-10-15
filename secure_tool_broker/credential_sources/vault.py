"""HashiCorp Vault KV credential source adapter for broker-owned automations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Mapping

from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.credential_state import credential_owner_environment
from secure_tool_broker.credential_sources.base import (
    CredentialMetadata,
    CredentialRef,
    CredentialSearchRequest,
    CredentialSecretRequest,
    GITHUB_AUTH_AUTOMATION,
    SecretFieldMetadata,
)
from secure_tool_broker.redaction import redact_text
from secure_tools.providers import ToolResult


VAULT_AUTH_REQUIRED_PATTERNS = (
    "permission denied",
    "missing client token",
    "bad token",
    "invalid token",
    "no vault token",
    "authentication required",
    "unauthorized",
    "status code 401",
    "status code 403",
)


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    stdin_kwargs: dict[str, Any]
    if input_text is None:
        stdin_kwargs = {"stdin": subprocess.DEVNULL}
    else:
        stdin_kwargs = {"input": input_text}
    try:
        return subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(env=env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            **stdin_kwargs,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"vault command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"vault command failed to start: {exc}")


def _string(value: Any) -> str:
    return str(value or "").strip()


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _default_mount() -> str:
    return _string(os.getenv("VAULT_KV_MOUNT")) or "secret"


def _normalize_folder(value: str | None) -> str:
    text = _string(value)
    return text.strip("/")


def _join_path(folder: str | None, key: str) -> str:
    folder_text = _normalize_folder(folder)
    key_text = _string(key).strip("/")
    if not folder_text:
        return key_text
    if not key_text:
        return folder_text
    return f"{folder_text}/{key_text}"


def _auth_required_result(content: Mapping[str, Any] | None = None) -> ToolResult:
    message = (
        "vault authentication is required in the secure tool broker. "
        "Ask the user to run /auth vault or configure broker-side Vault credentials."
    )
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": "vault",
            "auth_required": True,
            "auth_message": message,
            "user_action": "/auth vault",
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def _is_auth_required(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in VAULT_AUTH_REQUIRED_PATTERNS)


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else command


def _extract_kv_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        nested = data.get("data")
        if isinstance(nested, Mapping):
            return nested
        return data
    return {}


def _revision_date(payload: Mapping[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, Mapping):
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            return _string(metadata.get("created_time") or metadata.get("updated_time")) or None
    return None


def _metadata_from_path(
    *,
    path: str,
    mount: str,
    folder: str | None,
    fields: tuple[str, ...],
    revision_date: str | None = None,
) -> CredentialMetadata:
    secret_fields = tuple(
        SecretFieldMetadata(
            field_ref=field,
            label=field,
            kind="custom",
            secret_value_present=True,
        )
        for field in fields
        if field
    )
    supported_automations = (GITHUB_AUTH_AUTOMATION.to_dict(),) if secret_fields else ()
    return CredentialMetadata(
        credential_ref=CredentialRef(
            source_provider="vault",
            item_id=path,
            item_name=path.rsplit("/", 1)[-1] if path else "",
            vault=mount,
            folder=folder,
        ),
        display_name=path,
        username=None,
        username_present=False,
        urls=(),
        domains=(),
        vault=mount,
        folder=folder,
        tags=(),
        item_type="kv",
        revision_date=revision_date,
        secret_fields=secret_fields,
        supported_automations=supported_automations,
    )


class VaultCredentialSource:
    name = "vault"

    def auth_status(self) -> AuthStatus:
        owner_env = credential_owner_environment()
        executable_path = shutil.which("vault")
        configured = bool(owner_env.get("VAULT_ADDR") and owner_env.get("VAULT_TOKEN"))
        if not executable_path:
            return AuthStatus(
                provider="vault",
                status="unavailable",
                authenticated=False,
                message="Vault CLI is not installed or not on PATH: vault.",
                auth_options=("vault_env",),
            )
        completed = _run_command(
            [executable_path, "token", "lookup", "-format", "json"],
            env=owner_env,
            timeout_seconds=15,
        )
        if not isinstance(completed, ToolResult) and completed.returncode == 0:
            return AuthStatus(
                provider="vault",
                status="authenticated",
                authenticated=True,
                message="Vault token lookup succeeded for the secure tool broker.",
                auth_options=("vault_env",),
                details={"vault_addr_configured": bool(owner_env.get("VAULT_ADDR")), "kv_mount": _default_mount()},
            )
        return AuthStatus(
            provider="vault",
            status="configured" if configured else "auth_required",
            authenticated=False,
            message=(
                "Vault broker credentials are configured but not verified by this status check."
                if configured
                else "Vault authentication is required. Configure VAULT_ADDR and VAULT_TOKEN for the broker."
            ),
            auth_options=("vault_env",),
            details={"vault_addr_configured": bool(owner_env.get("VAULT_ADDR")), "kv_mount": _default_mount()} if configured else None,
        )

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        executable_path = shutil.which("vault")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: vault")
        mount = request.vault or _default_mount()
        folder = _normalize_folder(request.folder)
        command = [executable_path, "kv", "list", "-format", "json", f"-mount={mount}"]
        if folder:
            command.append(folder)
        completed = _run_command(command, timeout_seconds=30)
        content: dict[str, Any] = {
            "provider": "vault",
            "action": "credential.search",
            "query": request.query,
            "exit_code": getattr(completed, "returncode", None),
            "command": _command_preview(command),
        }
        if isinstance(completed, ToolResult):
            return completed
        stderr = redact_text(completed.stderr or "").strip()
        if stderr:
            content["stderr"] = stderr
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result(content)
            return ToolResult(status="error", content=content, error=f"vault exited with code {completed.returncode}")
        try:
            parsed = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", content=content, error=f"vault returned non-JSON key list output: {exc}")
        if isinstance(parsed, Mapping):
            raw_keys = parsed.get("keys", [])
        else:
            raw_keys = parsed
        if not isinstance(raw_keys, list):
            return ToolResult(status="error", content=content, error="vault kv list output did not contain a JSON key list.")
        query = request.query.lower()
        keys = [_string(key) for key in raw_keys if _string(key)]
        if query:
            keys = [key for key in keys if query in key.lower() or query in _join_path(folder, key).lower()]
        bounded_limit = _bounded_limit(request.limit)
        credentials = [
            _metadata_from_path(path=_join_path(folder, key).rstrip("/"), mount=mount, folder=folder or None, fields=()).to_dict()
            for key in keys[:bounded_limit]
        ]
        content.update({"count": len(keys), "returned_count": len(credentials), "credentials": credentials})
        return ToolResult(status="success", content=content)

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        item = self._get_secret_json(ref)
        if isinstance(item, ToolResult):
            return item
        mount = ref.vault or _default_mount()
        path = _entry_path(ref)
        fields = tuple(sorted(_extract_kv_data(item).keys()))
        return ToolResult(
            status="success",
            content={
                "provider": "vault",
                "action": "credential.get_metadata",
                "credential": _metadata_from_path(
                    path=path,
                    mount=mount,
                    folder=ref.folder,
                    fields=fields,
                    revision_date=_revision_date(item),
                ).to_dict(),
            },
        )

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        executable_path = shutil.which("vault")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: vault")
        mount = request.ref.vault or _default_mount()
        path = _entry_path(request.ref)
        if not path:
            return ToolResult(status="error", error="Vault item_id or item_name is required.")
        field_ref = _string(request.field_ref or "password")
        command = [executable_path, "kv", "get", f"-mount={mount}", f"-field={field_ref}", path]
        completed = _run_command(command, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"vault exited with code {completed.returncode}",
            )
        secret = completed.stdout.strip()
        if not secret:
            return ToolResult(status="error", error=f"Vault secret does not contain a non-empty field named {field_ref!r}.")
        return secret

    def _get_secret_json(self, ref: CredentialRef) -> Mapping[str, Any] | ToolResult:
        executable_path = shutil.which("vault")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: vault")
        mount = ref.vault or _default_mount()
        path = _entry_path(ref)
        if not path:
            return ToolResult(status="error", error="Vault item_id or item_name is required.")
        command = [executable_path, "kv", "get", "-format", "json", f"-mount={mount}", path]
        completed = _run_command(command, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"vault exited with code {completed.returncode}",
            )
        try:
            parsed = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", error=f"vault returned non-JSON secret output: {exc}")
        if not isinstance(parsed, Mapping):
            return ToolResult(status="error", error="vault secret output was not a JSON object.")
        return parsed


def _entry_path(ref: CredentialRef) -> str:
    item_path = _string(ref.item_id or ref.item_name).strip("/")
    folder = _normalize_folder(ref.folder)
    if folder and item_path.startswith(f"{folder}/"):
        return item_path
    return _join_path(folder, item_path)
