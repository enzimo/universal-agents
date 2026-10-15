"""Bitwarden credential source adapter for broker-owned automations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Mapping
from urllib.parse import urlparse

from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.credential_state import (
    active_credential_owner_id,
    broker_credential_file,
    credential_owner_environment,
    write_private_broker_file,
)
from secure_tool_broker.bitwarden_cli import bitwarden_server_url_from_env, ensure_bitwarden_server_configured_from_env
from secure_tool_broker.credential_sources.base import (
    CredentialMetadata,
    CredentialRef,
    CredentialSearchRequest,
    CredentialSecretRequest,
    GITHUB_AUTH_AUTOMATION,
    SecretFieldMetadata,
)
from secure_tool_broker.redaction import parse_and_redact_stdout, redact_text
from secure_tools.providers import ToolResult


BITWARDEN_AUTH_REQUIRED_PATTERNS = (
    "not logged in",
    "unauthenticated",
    "vault is locked",
    "invalid session",
    "authentication required",
    "unauthorized",
)

BITWARDEN_TYPE_NAMES = {
    1: "login",
    2: "secure_note",
    3: "card",
    4: "identity",
}


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
        return ToolResult(status="error", error=f"bw command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"bw command failed to start: {exc}")


def _is_auth_required(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in BITWARDEN_AUTH_REQUIRED_PATTERNS)


def _auth_required_result(content: Mapping[str, Any] | None = None) -> ToolResult:
    message = (
        "bitwarden authentication is required in the secure tool broker. Ask the user to run /auth bitwarden; "
        "for self-hosted Bitwarden or Vaultwarden, run /auth bitwarden server <url> first."
    )
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": "bitwarden",
            "auth_required": True,
            "auth_message": message,
            "user_action": "/auth bitwarden",
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else command


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _string(value: Any) -> str:
    return str(value or "").strip()


def _hostname_from_url(url: str) -> str:
    text = url.strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").lower()


def _item_reference(ref: CredentialRef) -> str:
    return _string(ref.item_id or ref.item_name)


def _item_is_archived(item: Mapping[str, Any]) -> bool:
    return bool(item.get("deletedDate") or item.get("archived") or item.get("isDeleted"))


def _item_type_name(raw_type: Any) -> str | None:
    try:
        return BITWARDEN_TYPE_NAMES.get(int(raw_type), str(raw_type))
    except (TypeError, ValueError):
        return str(raw_type) if raw_type not in (None, "") else None


def _extract_secret_field(item: Mapping[str, Any], field_ref: str) -> str:
    field_name = _string(field_ref or "password")
    if not field_name or field_name == "password":
        login = item.get("login")
        if isinstance(login, Mapping):
            value = _string(login.get("password"))
            if value:
                return value
    fields = item.get("fields")
    if isinstance(fields, list):
        for candidate in fields:
            if not isinstance(candidate, Mapping):
                continue
            name = _string(candidate.get("name"))
            if name == field_name:
                value = _string(candidate.get("value"))
                if value:
                    return value
    raise ValueError(f"Bitwarden item does not contain a non-empty secret field named {field_name!r}.")


def _secret_field_metadata(item: Mapping[str, Any]) -> tuple[SecretFieldMetadata, ...]:
    fields: list[SecretFieldMetadata] = []
    login = item.get("login")
    if isinstance(login, Mapping) and _string(login.get("password")):
        fields.append(
            SecretFieldMetadata(
                field_ref="password",
                label="password",
                kind="password",
                secret_value_present=True,
            )
        )
    item_fields = item.get("fields")
    if isinstance(item_fields, list):
        for candidate in item_fields:
            if not isinstance(candidate, Mapping):
                continue
            name = _string(candidate.get("name"))
            value = _string(candidate.get("value"))
            if not name or not value:
                continue
            fields.append(
                SecretFieldMetadata(
                    field_ref=name,
                    label=name,
                    kind="custom",
                    secret_value_present=True,
                )
            )
    return tuple(sorted(fields, key=lambda field: field.field_ref))


def _safe_string_tuple(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(_string(value) for value in values if _string(value))


def _item_urls(item: Mapping[str, Any]) -> tuple[str, ...]:
    login = item.get("login")
    if not isinstance(login, Mapping):
        return ()
    uris = login.get("uris")
    if not isinstance(uris, list):
        return ()
    urls: list[str] = []
    for uri in uris:
        if isinstance(uri, Mapping):
            value = _string(uri.get("uri"))
            if value:
                urls.append(value)
    return tuple(urls)


def _metadata_from_item(item: Mapping[str, Any]) -> CredentialMetadata:
    login = item.get("login")
    username = _string(login.get("username")) if isinstance(login, Mapping) else ""
    urls = _item_urls(item)
    domains = tuple(sorted({domain for domain in (_hostname_from_url(url) for url in urls) if domain}))
    secret_fields = _secret_field_metadata(item)
    supported_automations = (GITHUB_AUTH_AUTOMATION.to_dict(),) if secret_fields else ()
    return CredentialMetadata(
        credential_ref=CredentialRef(
            source_provider="bitwarden",
            item_id=_string(item.get("id")) or None,
            item_name=_string(item.get("name")) or None,
        ),
        display_name=_string(item.get("name")),
        username=username or None,
        username_present=bool(username),
        urls=urls,
        domains=domains,
        vault=None,
        folder=_string(item.get("folderId")) or None,
        tags=_safe_string_tuple(item.get("tags")),
        item_type=_item_type_name(item.get("type")),
        revision_date=_string(item.get("revisionDate")) or None,
        secret_fields=secret_fields,
        supported_automations=supported_automations,
    )


class BitwardenCredentialSource:
    name = "bitwarden"

    def auth_status(self) -> AuthStatus:
        owner_env = credential_owner_environment()
        configured = bool(owner_env.get("BW_SESSION"))
        try:
            configured_server_url = bitwarden_server_url_from_env(owner_env)
        except ValueError as exc:
            return AuthStatus(
                provider="bitwarden",
                status="unavailable",
                authenticated=False,
                message=str(exc),
                auth_options=("session_env", "session_form", "server_url"),
            )
        if configured and shutil.which("bw"):
            status_result = self._status_with_session(owner_env.get("BW_SESSION", ""))
            if status_result.status == "success":
                return AuthStatus(
                    provider="bitwarden",
                    status="authenticated",
                    authenticated=True,
                    message="Bitwarden broker session is unlocked.",
                    auth_options=("session_env", "session_form"),
                    details=status_result.content,
                )
            details = dict(status_result.content or {})
            if configured_server_url:
                details["configured_server_url"] = configured_server_url
            return AuthStatus(
                provider="bitwarden",
                status="configured",
                authenticated=False,
                message=str(status_result.error or "Bitwarden broker session is configured but not unlocked."),
                auth_options=("session_env", "session_form", "server_url"),
                details=details,
            )
        return AuthStatus(
            provider="bitwarden",
            status="configured" if configured else "auth_required",
            authenticated=False,
            message=(
                "Bitwarden broker session is configured but not verified by this status check."
                if configured
                else "Bitwarden authentication is required. Configure BW_SESSION or BW_SESSION_FILE for the broker."
            ),
            auth_options=("session_env", "session_form", "server_url"),
            details={"configured_server_url": configured_server_url} if configured_server_url else None,
        )

    def _status_with_session(self, session: str) -> ToolResult:
        session_value = _string(session)
        if not session_value:
            return ToolResult(status="error", error="Bitwarden session is required.")
        executable_path = shutil.which("bw")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
        env = credential_owner_environment()
        env["BW_SESSION"] = session_value
        config_result = ensure_bitwarden_server_configured_from_env(env)
        if config_result is not None:
            return config_result
        completed = _run_command([executable_path, "status"], env=env, timeout_seconds=15)
        if isinstance(completed, ToolResult):
            return completed
        parsed_json, redacted_stdout = parse_and_redact_stdout(completed.stdout or "")
        content = {
            "provider": "bitwarden",
            "action": "status",
            "exit_code": completed.returncode,
            "command": [executable_path, "status"],
        }
        if parsed_json is not None:
            content["json"] = parsed_json
        elif redacted_stdout:
            content["stdout"] = redacted_stdout
        stderr = redact_text(completed.stderr or "").strip()
        if stderr:
            content["stderr"] = stderr
        if completed.returncode != 0:
            return ToolResult(status="error", content=content, error=f"bw exited with code {completed.returncode}")
        status = ""
        if isinstance(parsed_json, dict):
            status = _string(parsed_json.get("status")).lower()
        if status and status != "unlocked":
            return ToolResult(status="error", content=content, error=f"Bitwarden vault is {status}, not unlocked.")
        return ToolResult(status="success", content=content)

    def login_with_session(self, session: str) -> ToolResult:
        result = self._status_with_session(session)
        if result.status != "success":
            return result
        if active_credential_owner_id():
            session_path = broker_credential_file("bitwarden", "session")
            if session_path is not None:
                write_private_broker_file(session_path, _string(session))
        else:
            os.environ["BW_SESSION"] = _string(session)
        return result

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        executable_path = shutil.which("bw")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
        env = credential_owner_environment()
        if not env.get("BW_SESSION"):
            return _auth_required_result()
        config_result = ensure_bitwarden_server_configured_from_env(env)
        if config_result is not None:
            return config_result
        command = [executable_path, "list", "items"]
        if request.query:
            command.extend(["--search", request.query])
        completed = _run_command(command, env=env, timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        content: dict[str, Any] = {
            "provider": "bitwarden",
            "action": "credential.search",
            "exit_code": completed.returncode,
            "command": _command_preview(command),
            "query": request.query,
        }
        stderr = redact_text(completed.stderr or "").strip()
        if stderr:
            content["stderr"] = stderr
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result(content)
            return ToolResult(status="error", content=content, error=f"bw exited with code {completed.returncode}")
        try:
            parsed = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", content=content, error=f"bw returned non-JSON item list output: {exc}")
        if not isinstance(parsed, list):
            return ToolResult(status="error", content=content, error="bw item list output was not a JSON array.")
        items = [item for item in parsed if isinstance(item, Mapping)]
        if not request.include_archived:
            items = [item for item in items if not _item_is_archived(item)]
        credentials = [_metadata_from_item(item).to_dict() for item in items]
        bounded_limit = _bounded_limit(request.limit)
        content.update(
            {
                "count": len(credentials),
                "returned_count": min(len(credentials), bounded_limit),
                "credentials": credentials[:bounded_limit],
            }
        )
        return ToolResult(status="success", content=content)

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        item = self._get_item_json(ref)
        if isinstance(item, ToolResult):
            return item
        metadata = _metadata_from_item(item)
        return ToolResult(
            status="success",
            content={
                "provider": "bitwarden",
                "action": "credential.get_metadata",
                "credential": metadata.to_dict(),
            },
        )

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        item = self._get_item_json(request.ref)
        if isinstance(item, ToolResult):
            return item
        try:
            return _extract_secret_field(item, request.field_ref)
        except Exception as exc:
            return ToolResult(status="error", error=str(exc))

    def _get_item_json(self, ref: CredentialRef) -> Mapping[str, Any] | ToolResult:
        reference = _item_reference(ref)
        if not reference:
            return ToolResult(status="error", error="Bitwarden item_id or item_name is required.")
        executable_path = shutil.which("bw")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
        env = credential_owner_environment()
        if not env.get("BW_SESSION"):
            return _auth_required_result()
        config_result = ensure_bitwarden_server_configured_from_env(env)
        if config_result is not None:
            return config_result
        completed = _run_command(
            [executable_path, "get", "item", reference],
            env=env,
            timeout_seconds=30,
        )
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"bw exited with code {completed.returncode}",
            )
        try:
            parsed = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", error=f"bw returned non-JSON item output: {exc}")
        if not isinstance(parsed, Mapping):
            return ToolResult(status="error", error="bw item output was not a JSON object.")
        return parsed
