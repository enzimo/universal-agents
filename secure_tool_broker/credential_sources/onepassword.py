"""1Password credential source adapter for broker-owned automations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Mapping
from urllib.parse import quote, urlparse

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


ONEPASSWORD_AUTH_REQUIRED_PATTERNS = (
    "not signed in",
    "not currently signed in",
    "authentication required",
    "sign in",
    "session expired",
    "unauthorized",
)

SECRET_FIELD_TYPES = {"concealed", "otp", "password", "totp"}


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
        return ToolResult(status="error", error=f"op command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"op command failed to start: {exc}")


def _string(value: Any) -> str:
    return str(value or "").strip()


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _auth_required_result(content: Mapping[str, Any] | None = None) -> ToolResult:
    message = (
        "onepassword authentication is required in the secure tool broker. "
        "Ask the user to run /auth onepassword or configure broker-side 1Password CLI auth."
    )
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": "onepassword",
            "auth_required": True,
            "auth_message": message,
            "user_action": "/auth onepassword",
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def _is_auth_required(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in ONEPASSWORD_AUTH_REQUIRED_PATTERNS)


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else command


def _has_op_session_env(env: Mapping[str, str] | None = None) -> bool:
    source = env or os.environ
    return any(key.startswith("OP_SESSION_") and value for key, value in source.items())


def _has_noninteractive_op_auth(env: Mapping[str, str] | None = None) -> bool:
    source = env or os.environ
    if source.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return True
    if source.get("OP_CONNECT_HOST") and source.get("OP_CONNECT_TOKEN"):
        return True
    return _has_op_session_env(source)


def _hostname_from_url(url: str) -> str:
    text = url.strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").lower()


def _list_string_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(_string(value) for value in values if _string(value))


def _vault_value(item: Mapping[str, Any]) -> str | None:
    vault = item.get("vault")
    if isinstance(vault, Mapping):
        return _string(vault.get("name")) or _string(vault.get("id")) or None
    return _string(vault) or None


def _item_urls(item: Mapping[str, Any]) -> tuple[str, ...]:
    urls: list[str] = []
    for key in ("urls", "websites"):
        raw_urls = item.get(key)
        if not isinstance(raw_urls, list):
            continue
        for raw_url in raw_urls:
            if isinstance(raw_url, Mapping):
                value = _string(raw_url.get("href") or raw_url.get("url") or raw_url.get("uri"))
            else:
                value = _string(raw_url)
            if value:
                urls.append(value)
    primary_url = _string(item.get("url"))
    if primary_url:
        urls.append(primary_url)
    return tuple(dict.fromkeys(urls))


def _field_label(field: Mapping[str, Any]) -> str:
    return (
        _string(field.get("label"))
        or _string(field.get("id"))
        or _string(field.get("purpose"))
        or _string(field.get("type"))
    )


def _field_ref(field: Mapping[str, Any]) -> str:
    return _string(field.get("id")) or _field_label(field)


def _is_secret_field(field: Mapping[str, Any]) -> bool:
    purpose = _string(field.get("purpose")).upper()
    field_type = _string(field.get("type")).lower()
    label = _field_label(field).lower()
    if purpose in {"PASSWORD", "OTP"}:
        return True
    if field_type in SECRET_FIELD_TYPES:
        return True
    return label in {"password", "passcode", "token", "api key", "api_key", "secret"}


def _username_from_item(item: Mapping[str, Any]) -> str | None:
    fields = item.get("fields")
    if not isinstance(fields, list):
        return None
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        purpose = _string(field.get("purpose")).upper()
        label = _field_label(field).lower()
        if purpose == "USERNAME" or label in {"username", "email", "login"}:
            value = _string(field.get("value"))
            if value:
                return value
    return None


def _secret_field_metadata(item: Mapping[str, Any]) -> tuple[SecretFieldMetadata, ...]:
    fields = item.get("fields")
    if not isinstance(fields, list):
        return ()
    secret_fields: list[SecretFieldMetadata] = []
    for field in fields:
        if not isinstance(field, Mapping) or not _is_secret_field(field):
            continue
        field_ref = _field_ref(field)
        label = _field_label(field)
        if not field_ref or not label:
            continue
        kind = "password" if _string(field.get("purpose")).upper() == "PASSWORD" or label.lower() == "password" else "custom"
        secret_fields.append(
            SecretFieldMetadata(
                field_ref=field_ref,
                label=label,
                kind=kind,
                secret_value_present=bool(_string(field.get("value")) or field.get("value") is not None),
            )
        )
    return tuple(sorted(secret_fields, key=lambda field: field.field_ref))


def _metadata_from_item(item: Mapping[str, Any]) -> CredentialMetadata:
    urls = _item_urls(item)
    domains = tuple(sorted({domain for domain in (_hostname_from_url(url) for url in urls) if domain}))
    username = _username_from_item(item)
    secret_fields = _secret_field_metadata(item)
    vault = _vault_value(item)
    supported_automations = (GITHUB_AUTH_AUTOMATION.to_dict(),) if secret_fields else ()
    return CredentialMetadata(
        credential_ref=CredentialRef(
            source_provider="onepassword",
            item_id=_string(item.get("id")) or None,
            item_name=_string(item.get("title") or item.get("name")) or None,
            vault=vault,
        ),
        display_name=_string(item.get("title") or item.get("name") or item.get("id")),
        username=username,
        username_present=bool(username),
        urls=urls,
        domains=domains,
        vault=vault,
        folder=None,
        tags=_list_string_values(item.get("tags")),
        item_type=_string(item.get("category")) or None,
        revision_date=_string(item.get("updated_at") or item.get("updatedAt") or item.get("last_edited_by")) or None,
        secret_fields=secret_fields,
        supported_automations=supported_automations,
    )


def _item_reference(ref: CredentialRef) -> str:
    return _string(ref.item_id or ref.item_name)


def _matches_query(item: Mapping[str, Any], query: str) -> bool:
    if not query:
        return True
    needle = query.lower()
    values: list[str] = [
        _string(item.get("id")),
        _string(item.get("title") or item.get("name")),
        _string(item.get("category")),
        _vault_value(item) or "",
    ]
    values.extend(_item_urls(item))
    values.extend(_list_string_values(item.get("tags")))
    return any(needle in value.lower() for value in values if value)


def _op_secret_reference(*, vault: str | None, item: str, field: str) -> str | None:
    if not vault or not item or not field:
        return None
    return f"op://{quote(vault, safe='')}/{quote(item, safe='')}/{quote(field, safe='')}"


class OnePasswordCredentialSource:
    name = "onepassword"

    def auth_status(self) -> AuthStatus:
        owner_env = credential_owner_environment()
        executable_path = shutil.which("op")
        if not executable_path:
            return AuthStatus(
                provider="onepassword",
                status="unavailable",
                authenticated=False,
                message="1Password CLI is not installed or not on PATH: op.",
                auth_options=("service_account_env", "signed_in_cli"),
            )
        noninteractive_configured = _has_noninteractive_op_auth(owner_env)
        account_hint_configured = bool(owner_env.get("OP_ACCOUNT"))
        if not noninteractive_configured:
            return AuthStatus(
                provider="onepassword",
                status="configured" if account_hint_configured else "auth_required",
                authenticated=False,
                message=(
                    "1Password account selection is configured, but no non-interactive broker auth token or session is available. "
                    "/auth status skipped the 1Password CLI probe so op cannot start interactive account setup."
                    if account_hint_configured
                    else "1Password authentication is required. Configure OP_SERVICE_ACCOUNT_TOKEN, OP_CONNECT_HOST/OP_CONNECT_TOKEN, or an OP_SESSION_* value for the broker. /auth status will not start interactive 1Password account setup."
                ),
                auth_options=("service_account_env", "connect_env", "session_env"),
                details={
                    "status_check_skipped": True,
                    "reason": "missing_noninteractive_auth_env",
                    "op_account_configured": account_hint_configured,
                },
            )
        completed = _run_command(
            [executable_path, "vault", "list", "--format", "json"],
            env=owner_env,
            timeout_seconds=15,
        )
        if not isinstance(completed, ToolResult) and completed.returncode == 0:
            return AuthStatus(
                provider="onepassword",
                status="authenticated",
                authenticated=True,
                message="1Password CLI authentication is available to the secure tool broker.",
                auth_options=("service_account_env", "signed_in_cli"),
            )
        return AuthStatus(
            provider="onepassword",
            status="configured",
            authenticated=False,
            message=(
                "1Password broker credentials are configured but were not verified by this status check. "
                "Refresh the broker-side service account, Connect token, or OP_SESSION_* value."
            ),
            auth_options=("service_account_env", "connect_env", "session_env"),
        )

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        executable_path = shutil.which("op")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: op")
        command = [executable_path, "item", "list", "--format", "json"]
        if request.vault:
            command.extend(["--vault", request.vault])
        if request.include_archived:
            command.append("--include-archive")
        completed = _run_command(command, timeout_seconds=30)
        content: dict[str, Any] = {
            "provider": "onepassword",
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
            return ToolResult(status="error", content=content, error=f"op exited with code {completed.returncode}")
        try:
            parsed = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", content=content, error=f"op returned non-JSON item list output: {exc}")
        if not isinstance(parsed, list):
            return ToolResult(status="error", content=content, error="op item list output was not a JSON array.")

        bounded_limit = _bounded_limit(request.limit)
        summary_items = [item for item in parsed if isinstance(item, Mapping) and _matches_query(item, request.query)]
        credentials: list[dict[str, Any]] = []
        for item in summary_items[:bounded_limit]:
            ref = CredentialRef(
                source_provider="onepassword",
                item_id=_string(item.get("id")) or None,
                item_name=_string(item.get("title") or item.get("name")) or None,
                vault=request.vault or _vault_value(item),
            )
            detail = self._get_item_json(ref, include_archived=request.include_archived)
            metadata_source = detail if isinstance(detail, Mapping) else item
            credentials.append(_metadata_from_item(metadata_source).to_dict())
        content.update(
            {
                "count": len(summary_items),
                "returned_count": len(credentials),
                "credentials": credentials,
            }
        )
        return ToolResult(status="success", content=content)

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        item = self._get_item_json(ref)
        if isinstance(item, ToolResult):
            return item
        return ToolResult(
            status="success",
            content={
                "provider": "onepassword",
                "action": "credential.get_metadata",
                "credential": _metadata_from_item(item).to_dict(),
            },
        )

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        reference = _item_reference(request.ref)
        field_ref = _string(request.field_ref or "password")
        if reference.startswith("op://"):
            secret = self._read_secret_reference(reference)
            if secret:
                return secret
        secret_reference = _op_secret_reference(vault=request.ref.vault, item=reference, field=field_ref)
        if secret_reference:
            secret = self._read_secret_reference(secret_reference)
            if isinstance(secret, ToolResult):
                return secret
            if secret:
                return secret
        item = self._get_item_json(request.ref, reveal=True)
        if isinstance(item, ToolResult):
            return item
        try:
            return _extract_secret_field(item, field_ref)
        except Exception as exc:
            return ToolResult(status="error", error=str(exc))

    def _read_secret_reference(self, reference: str) -> str | ToolResult:
        executable_path = shutil.which("op")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: op")
        completed = _run_command([executable_path, "read", "--no-newline", reference], timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"op exited with code {completed.returncode}",
            )
        return completed.stdout or ""

    def _get_item_json(
        self,
        ref: CredentialRef,
        *,
        include_archived: bool = False,
        reveal: bool = False,
    ) -> Mapping[str, Any] | ToolResult:
        reference = _item_reference(ref)
        if not reference:
            return ToolResult(status="error", error="1Password item_id or item_name is required.")
        executable_path = shutil.which("op")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: op")
        command = [executable_path, "item", "get", reference, "--format", "json"]
        if ref.vault:
            command.extend(["--vault", ref.vault])
        if include_archived:
            command.append("--include-archive")
        if reveal:
            command.append("--reveal")
        completed = _run_command(command, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"op exited with code {completed.returncode}",
            )
        try:
            parsed = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", error=f"op returned non-JSON item output: {exc}")
        if not isinstance(parsed, Mapping):
            return ToolResult(status="error", error="op item output was not a JSON object.")
        return parsed


def _extract_secret_field(item: Mapping[str, Any], field_ref: str) -> str:
    wanted = _string(field_ref or "password")
    fields = item.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, Mapping):
                continue
            candidates = {
                _string(field.get("id")),
                _field_label(field),
                _string(field.get("purpose")).lower(),
            }
            candidates = {candidate.lower() for candidate in candidates if candidate}
            if wanted.lower() in candidates or (wanted == "password" and "password" in candidates):
                value = _string(field.get("value"))
                if value:
                    return value
    raise ValueError(f"1Password item does not contain a non-empty secret field named {wanted!r}.")
