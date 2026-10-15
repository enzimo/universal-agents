"""KeePass/KeePassXC credential source adapter for broker-owned automations."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlparse

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


KEEPASS_AUTH_REQUIRED_PATTERNS = (
    "invalid credentials",
    "failed to open",
    "wrong key",
    "wrong password",
    "database password",
    "unable to open",
    "no such file",
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
        return ToolResult(status="error", error=f"keepassxc-cli command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"keepassxc-cli command failed to start: {exc}")


def _string(value: Any) -> str:
    return str(value or "").strip()


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _database_path() -> str:
    env = credential_owner_environment()
    return _string(env.get("KEEPASSXC_DATABASE") or env.get("KEEPASS_DATABASE"))


def _database_password() -> str:
    env = credential_owner_environment()
    return str(env.get("KEEPASSXC_PASSWORD") or env.get("KEEPASS_PASSWORD") or "")


def _key_file_path() -> str:
    env = credential_owner_environment()
    return _string(env.get("KEEPASSXC_KEY_FILE") or env.get("KEEPASS_KEY_FILE"))


def _no_password() -> bool:
    env = credential_owner_environment()
    raw = _string(env.get("KEEPASSXC_NO_PASSWORD") or env.get("KEEPASS_NO_PASSWORD")).lower()
    return raw in {"1", "true", "yes", "on"}


def _command_with_database(
    executable_path: str,
    command: str,
    database: str,
    *entry_args: str,
    options: tuple[str, ...] = (),
) -> list[str]:
    cli_command = [executable_path, command, "-q"]
    key_file = _key_file_path()
    if key_file:
        cli_command.extend(["--key-file", key_file])
    if _no_password():
        cli_command.append("--no-password")
    cli_command.extend(option for option in options if option)
    cli_command.append(database)
    cli_command.extend(arg for arg in entry_args if arg)
    return cli_command


def _password_input() -> str | None:
    if _no_password():
        return None
    password = _database_password()
    return f"{password}\n" if password else None


def _auth_required_result(content: Mapping[str, Any] | None = None) -> ToolResult:
    message = (
        "keepass authentication is required in the secure tool broker. "
        "Ask the user to run /auth keepass or configure broker-side KeePassXC database credentials."
    )
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": "keepass",
            "auth_required": True,
            "auth_message": message,
            "user_action": "/auth keepass",
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def _is_auth_required(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in KEEPASS_AUTH_REQUIRED_PATTERNS)


def _command_preview(command: list[str]) -> list[str]:
    if len(command) <= 3:
        return command
    return [command[0], command[1], "..."]


def _hostname_from_url(url: str) -> str:
    text = url.strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").lower()


def _entry_reference(ref: CredentialRef) -> str:
    return _string(ref.item_id or ref.item_name)


def _entry_folder(path: str) -> str | None:
    parent = str(PurePosixPath(path).parent)
    return None if parent in {"", "."} else parent


def _entry_display_name(path: str, title: str | None = None) -> str:
    return _string(title) or PurePosixPath(path).name or path


def _parse_attribute_lines(stdout: str, attributes: tuple[str, ...]) -> dict[str, str]:
    lines = stdout.splitlines()
    parsed: dict[str, str] = {}
    for index, attribute in enumerate(attributes):
        parsed[attribute] = lines[index].strip() if index < len(lines) else ""
    return parsed


def _metadata_from_entry(path: str, attributes: Mapping[str, str], *, database: str) -> CredentialMetadata:
    title = _string(attributes.get("Title"))
    username = _string(attributes.get("UserName"))
    url = _string(attributes.get("URL"))
    urls = (url,) if url else ()
    domains = tuple(sorted({domain for domain in (_hostname_from_url(value) for value in urls) if domain}))
    secret_fields = (
        SecretFieldMetadata(
            field_ref="Password",
            label="Password",
            kind="password",
            secret_value_present=True,
        ),
    )
    return CredentialMetadata(
        credential_ref=CredentialRef(
            source_provider="keepass",
            item_id=path,
            item_name=_entry_display_name(path, title),
            vault=database,
            folder=_entry_folder(path),
        ),
        display_name=_entry_display_name(path, title),
        username=username or None,
        username_present=bool(username),
        urls=urls,
        domains=domains,
        vault=database,
        folder=_entry_folder(path),
        tags=(),
        item_type="login",
        revision_date=None,
        secret_fields=secret_fields,
        supported_automations=(GITHUB_AUTH_AUTOMATION.to_dict(),),
    )


class KeepassCredentialSource:
    name = "keepass"

    def auth_status(self) -> AuthStatus:
        executable_path = shutil.which("keepassxc-cli")
        database = _database_path()
        configured = bool(database and (_database_password() or _key_file_path() or _no_password()))
        if not executable_path:
            return AuthStatus(
                provider="keepass",
                status="unavailable",
                authenticated=False,
                message="KeePassXC CLI is not installed or not on PATH: keepassxc-cli.",
                auth_options=("database_env", "password_env", "key_file_env"),
            )
        if not database:
            return AuthStatus(
                provider="keepass",
                status="auth_required",
                authenticated=False,
                message="KeePass database path is required. Configure KEEPASSXC_DATABASE for the broker.",
                auth_options=("database_env", "password_env", "key_file_env"),
            )
        command = _command_with_database(executable_path, "db-info", database)
        completed = _run_command(command, input_text=_password_input(), timeout_seconds=15)
        if not isinstance(completed, ToolResult) and completed.returncode == 0:
            return AuthStatus(
                provider="keepass",
                status="authenticated",
                authenticated=True,
                message="KeePassXC database can be opened by the secure tool broker.",
                auth_options=("database_env", "password_env", "key_file_env"),
                details={"database_configured": True, "key_file_configured": bool(_key_file_path())},
            )
        return AuthStatus(
            provider="keepass",
            status="configured" if configured else "auth_required",
            authenticated=False,
            message=(
                "KeePassXC database credentials are configured but not verified by this status check."
                if configured
                else "KeePass authentication is required. Configure KEEPASSXC_PASSWORD, KEEPASSXC_KEY_FILE, or KEEPASSXC_NO_PASSWORD."
            ),
            auth_options=("database_env", "password_env", "key_file_env"),
            details={"database_configured": bool(database), "key_file_configured": bool(_key_file_path())},
        )

    def search(self, request: CredentialSearchRequest) -> ToolResult:
        executable_path = shutil.which("keepassxc-cli")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: keepassxc-cli")
        database = request.vault or _database_path()
        if not database:
            return _auth_required_result({"provider": "keepass", "action": "credential.search"})
        if request.query:
            command = _command_with_database(executable_path, "search", database, request.query)
        else:
            folder = request.folder or ""
            command = _command_with_database(executable_path, "ls", database, folder, options=("-R", "-f"))
        completed = _run_command(command, input_text=_password_input(), timeout_seconds=30)
        content: dict[str, Any] = {
            "provider": "keepass",
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
            return ToolResult(status="error", content=content, error=f"keepassxc-cli exited with code {completed.returncode}")
        paths = [_string(line) for line in completed.stdout.splitlines() if _string(line)]
        bounded_limit = _bounded_limit(request.limit)
        credentials: list[dict[str, Any]] = []
        for path in paths[:bounded_limit]:
            metadata = self._metadata_for_entry(path, database=database)
            if isinstance(metadata, ToolResult):
                continue
            credentials.append(metadata.to_dict())
        content.update({"count": len(paths), "returned_count": len(credentials), "credentials": credentials})
        return ToolResult(status="success", content=content)

    def get_metadata(self, ref: CredentialRef) -> ToolResult:
        database = ref.vault or _database_path()
        if not database:
            return _auth_required_result({"provider": "keepass", "action": "credential.get_metadata"})
        entry = _entry_reference(ref)
        if not entry:
            return ToolResult(status="error", error="KeePass item_id or item_name is required.")
        metadata = self._metadata_for_entry(entry, database=database)
        if isinstance(metadata, ToolResult):
            return metadata
        return ToolResult(
            status="success",
            content={"provider": "keepass", "action": "credential.get_metadata", "credential": metadata.to_dict()},
        )

    def get_secret(self, request: CredentialSecretRequest) -> str | ToolResult:
        executable_path = shutil.which("keepassxc-cli")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: keepassxc-cli")
        database = request.ref.vault or _database_path()
        if not database:
            return _auth_required_result({"provider": "keepass", "action": "credential.get_secret"})
        entry = _entry_reference(request.ref)
        if not entry:
            return ToolResult(status="error", error="KeePass item_id or item_name is required.")
        attribute = _string(request.field_ref or "Password")
        if attribute.lower() == "password":
            attribute = "Password"
        command = _command_with_database(executable_path, "show", database, entry, options=("-s", "-a", attribute))
        completed = _run_command(command, input_text=_password_input(), timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"keepassxc-cli exited with code {completed.returncode}",
            )
        secret = completed.stdout.strip()
        if not secret:
            return ToolResult(status="error", error=f"KeePass entry does not contain a non-empty attribute named {attribute!r}.")
        return secret

    def _metadata_for_entry(self, entry: str, *, database: str) -> CredentialMetadata | ToolResult:
        executable_path = shutil.which("keepassxc-cli")
        if not executable_path:
            return ToolResult(status="error", error="Required CLI is not installed or not on PATH: keepassxc-cli")
        attributes = ("Title", "UserName", "URL")
        command = _command_with_database(
            executable_path,
            "show",
            database,
            entry,
            options=("-a", "Title", "-a", "UserName", "-a", "URL"),
        )
        completed = _run_command(command, input_text=_password_input(), timeout_seconds=30)
        if isinstance(completed, ToolResult):
            return completed
        if completed.returncode != 0:
            if _is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
                return _auth_required_result()
            return ToolResult(
                status="error",
                content={"stderr": redact_text(completed.stderr or "").strip()},
                error=f"keepassxc-cli exited with code {completed.returncode}",
            )
        return _metadata_from_entry(entry, _parse_attribute_lines(completed.stdout, attributes), database=database)
