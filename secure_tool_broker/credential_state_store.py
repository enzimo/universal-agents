"""Credential-state storage backends for broker-owned auth payloads."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from secure_tool_broker.defaults import (
    DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES,
    DEFAULT_CREDENTIAL_STATE_MODE,
    DEFAULT_CREDENTIAL_STATE_NAMESPACE,
    DEFAULT_CREDENTIAL_STATE_SOURCE,
)
from secure_tool_broker.credential_sources import (
    CredentialRef,
    CredentialSecretRequest,
    get_credential_source,
    normalize_credential_source_name,
)
from secure_tool_broker.bitwarden_cli import ensure_bitwarden_server_configured_from_env
from secure_tool_broker.credential_sources.vault import (
    _default_mount as _vault_default_mount,
)
from access_control import safe_open
from custom_messages.contracts.access_v2 import SignInService
from secure_tool_broker.credential_catalog import delete_sealbox_item
from secure_tool_broker.credential_state import (
    active_credential_owner_id,
    active_credential_owner_scope_kind,
    broker_credential_file,
    broker_credentials_root,
    person_credential_parts,
    credential_owner_environment,
    credential_owner_context,
    write_private_broker_file,
)
from secure_tool_broker.redaction import redact_text
from secure_tool_broker.sealbox_cli import (
    redacted_completed_content as sealbox_redacted_completed_content,
)
from secure_tool_broker.sealbox_cli import run_sealbox, safe_key_component
from secure_tool_broker.sealbox_tenants import SEALBOX_TENANT_NOT_PROVISIONED, sealbox_tenant_environment
from secure_tools.providers import ToolResult


logger = logging.getLogger(__name__)

STATE_KEY_PATHS: dict[str, tuple[str, ...]] = {
    "xurl.sessions_json": ("xurl", "sessions.json"),
    # A person's own X sign-in and the team's X app (secure_tool_broker.x_sign_in).
    "x.sign_in_json": ("xurl", "sign_in.json"),
    "x.oauth_client_json": ("xurl", "oauth_client.json"),
    "github.token": ("gh", "token"),
    "gog.token_json": ("gog", "token.json"),
    "gog.oauth_client_credentials_json": ("gog", "oauth_client_credentials.json"),
    # Model provider keys by endpoint address, for the team or one person
    # (secure_tool_broker.model_keys).
    "model.endpoint_keys_json": ("model", "endpoint_keys.json"),
}

# The service each key signs in to, so a removed person's reply names what was
# deleted. Every key in STATE_KEY_PATHS has one (tests check it).
STATE_KEY_SERVICES: dict[str, SignInService] = {
    "xurl.sessions_json": SignInService.X,
    "x.sign_in_json": SignInService.X,
    "x.oauth_client_json": SignInService.X,
    "github.token": SignInService.GITHUB,
    "gog.token_json": SignInService.GOOGLE,
    "gog.oauth_client_credentials_json": SignInService.GOOGLE,
    "model.endpoint_keys_json": SignInService.MODEL_KEYS,
}

# Set on a successful deletion: whether there was a value to delete.
STATE_FOUND = "credential_state_found"
_VAULT = "vault"
_BITWARDEN = "bitwarden"
_SEALBOX = "sealbox"
_FILES = "files"
_CREDENTIAL_SOURCE = "credential_source"
_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CredentialStateBackendConfig:
    mode: str = DEFAULT_CREDENTIAL_STATE_MODE
    source: str | None = DEFAULT_CREDENTIAL_STATE_SOURCE
    namespace: str = DEFAULT_CREDENTIAL_STATE_NAMESPACE
    fallback_to_files: bool = DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "CredentialStateBackendConfig":
        env_mapping = env if env is not None else os.environ
        mode = (
            str(
                env_mapping.get("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE")
                or DEFAULT_CREDENTIAL_STATE_MODE
            )
            .strip()
            .lower()
        )
        source = (
            str(
                env_mapping.get("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE")
                or DEFAULT_CREDENTIAL_STATE_SOURCE
            )
            .strip()
            .lower()
            or None
        )
        namespace = (
            str(
                env_mapping.get("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE")
                or DEFAULT_CREDENTIAL_STATE_NAMESPACE
            )
            .strip()
            .strip("/")
            or DEFAULT_CREDENTIAL_STATE_NAMESPACE
        )
        raw_fallback = env_mapping.get(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES"
        )
        fallback = (
            DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES
            if raw_fallback in (None, "")
            else str(raw_fallback).strip().lower() in {"1", "true", "yes", "on"}
        )
        return cls(
            mode=mode, source=source, namespace=namespace, fallback_to_files=fallback
        )


def _state_key_path(key: str) -> tuple[str, ...]:
    try:
        return STATE_KEY_PATHS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported broker credential-state key: {key}") from exc


def _file_path_for_key(key: str) -> Path | None:
    return broker_credential_file(*_state_key_path(key))


def _read_file_state(key: str) -> str | ToolResult | None:
    path = _file_path_for_key(key)
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _write_file_state(key: str, value: str, *, exclusive: bool = False) -> ToolResult:
    path = _file_path_for_key(key)
    if path is None:
        return ToolResult(
            status="success",
            content={
                "credential_state_backend": "files",
                "credential_state_key": key,
                "credential_state_persisted": False,
            },
        )
    write_private_broker_file(path, value, exclusive=exclusive)
    return ToolResult(
        status="success",
        content={
            "credential_state_backend": "files",
            "credential_state_file": str(path),
            "credential_state_key": key,
        },
    )


def credential_state_item_path(config: CredentialStateBackendConfig, key: str) -> str:
    provider, name = key.split(".", 1)
    owner_id = active_credential_owner_id()
    owner_component = ""
    if owner_id:
        import hashlib

        owner_component = (
            "owner-" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32] + "/"
        )
    scope_prefix = "teams/" if active_credential_owner_scope_kind() == "team" else ""
    return f"{config.namespace}/{scope_prefix}{owner_component}{provider}/{name}"


def credential_state_item_name(config: CredentialStateBackendConfig, key: str) -> str:
    return credential_state_item_path(config, key).replace("/", " :: ")


def credential_state_sealbox_key(config: CredentialStateBackendConfig, key: str) -> str:
    provider, name = key.split(".", 1)
    namespace = ":".join(
        safe_key_component(part, fallback="state")
        for part in config.namespace.split("/")
        if part
    )
    return ":".join(
        [
            namespace or DEFAULT_CREDENTIAL_STATE_NAMESPACE,
            safe_key_component(provider, fallback="provider"),
            safe_key_component(name, fallback="value"),
        ]
    )


def _read_credential_source_state(
    config: CredentialStateBackendConfig, key: str
) -> str | ToolResult | None:
    if not config.source:
        return ToolResult(
            status="error", error="Credential-state backend source is required."
        )
    source = get_credential_source(config.source)
    if source is None:
        return ToolResult(
            status="error",
            error=f"Credential source is not registered: {config.source}",
        )
    source_name = normalize_credential_source_name(config.source)
    ref = CredentialRef(
        source_provider=source_name,
        item_id=(
            credential_state_item_path(config, key)
            if source_name == "vault"
            else credential_state_sealbox_key(config, key)
            if source_name == "sealbox"
            else None
        ),
        item_name=credential_state_sealbox_key(config, key)
        if source_name == "sealbox"
        else credential_state_item_name(config, key),
        vault=_vault_default_mount() if source_name == "vault" else None,
        owner_scope_kind=active_credential_owner_scope_kind() if active_credential_owner_id() else None,
        owner_id=active_credential_owner_id(),
    )
    result = source.get_secret(
        CredentialSecretRequest(
            ref=ref,
            field_ref="value",
            owner_scope_kind=ref.owner_scope_kind,
            owner_id=ref.owner_id,
        )
    )
    if isinstance(result, ToolResult):
        error_code = result.content.get("error_code")
        # A fresh owner has no tenant until an authorized write provisions it.
        # Authentication or damaged material in an existing tenant is a failure,
        # not an empty record that a read-modify-write may replace.
        if source_name == _SEALBOX:
            return None if error_code in {SEALBOX_TENANT_NOT_PROVISIONED, "credential_not_found"} else result
        return None if error_code in {"auth_required", "credential_not_found"} else result
    return str(result).strip() if str(result).strip() else None


def _run_vault(command: list[str], *, action: str) -> subprocess.CompletedProcess[str] | ToolResult:
    """Run the Vault CLI with the active owner's environment; a ToolResult when it cannot run."""

    try:
        return subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"vault {action} timed out after {_TIMEOUT_SECONDS}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"vault {action} failed to start: {exc}")


def _write_vault_state(
    config: CredentialStateBackendConfig, key: str, value: str
) -> ToolResult:
    executable = shutil.which("vault")
    if not executable:
        return ToolResult(
            status="error", error="Required CLI is not installed or not on PATH: vault"
        )
    mount = _vault_default_mount()
    path = credential_state_item_path(config, key)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".secret", delete=False
    ) as handle:
        handle.write(value if value.endswith("\n") else f"{value}\n")
        temp_path = Path(handle.name)
    try:
        command = [
            executable,
            "kv",
            "put",
            f"-mount={mount}",
            path,
            f"value=@{temp_path}",
        ]
        completed = _run_vault(command, action="kv put")
    finally:
        temp_path.unlink(missing_ok=True)
    if isinstance(completed, ToolResult):
        return completed
    content = {
        "credential_state_backend": "credential_source",
        "credential_state_source": "vault",
        "credential_state_key": key,
        "credential_state_path": path,
        "vault_mount": mount,
        "exit_code": completed.returncode,
        "command": [
            executable,
            "kv",
            "put",
            f"-mount={mount}",
            path,
            "value=@[TEMP_FILE]",
        ],
    }
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            content=content,
            error=f"vault kv put exited with code {completed.returncode}",
        )
    return ToolResult(status="success", content=content)


def _run_bitwarden(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    stdin_kwargs = (
        {"stdin": subprocess.DEVNULL} if input_text is None else {"input": input_text}
    )
    try:
        return subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(),
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
        return ToolResult(
            status="error", error=f"bw command timed out after {timeout_seconds}s"
        )
    except Exception as exc:
        return ToolResult(status="error", error=f"bw command failed to start: {exc}")


def _bitwarden_item_payload(
    *, name: str, value: str, existing_item: Mapping[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = dict(existing_item or {})
    payload["type"] = 1
    payload["name"] = name
    payload["notes"] = "Secure tool broker credential-state item. Do not edit manually."
    login_value = payload.get("login")
    login: dict[object, object] = (
        dict(login_value) if isinstance(login_value, Mapping) else {}
    )
    login["username"] = "secure-tool-broker"
    login["password"] = value
    login.setdefault("uris", [])
    payload["login"] = login

    fields_value = payload.get("fields", [])
    if not isinstance(fields_value, list):
        raise ValueError("Bitwarden item fields must be a JSON array.")
    fields = [
        field
        for field in fields_value
        if isinstance(field, Mapping) and field.get("name") != "value"
    ]
    fields.append({"name": "value", "value": value, "type": 1})
    payload["fields"] = fields
    return payload


def _bitwarden_encode_item(
    executable: str, payload: Mapping[str, object]
) -> str | ToolResult:
    completed = _run_bitwarden(
        [executable, "encode"],
        input_text=json.dumps(payload, separators=(",", ":")),
        timeout_seconds=15,
    )
    if isinstance(completed, ToolResult):
        return completed
    if completed.returncode != 0:
        content = {
            "credential_state_backend": "credential_source",
            "credential_state_source": "bitwarden",
            "exit_code": completed.returncode,
            "command": [executable, "encode"],
        }
        stderr = redact_text(completed.stderr or "").strip()
        if stderr:
            content["stderr"] = stderr
        return ToolResult(
            status="error",
            content=content,
            error=f"bw encode exited with code {completed.returncode}",
        )
    encoded = str(completed.stdout or "").strip()
    if not encoded:
        return ToolResult(status="error", error="bw encode returned empty output.")
    return encoded


def _prepare_bitwarden_write(
    *, executable: str, item_name: str, value: str
) -> tuple[str, str | None] | ToolResult:
    get_completed = _run_bitwarden(
        [executable, "get", "item", item_name], timeout_seconds=30
    )
    if isinstance(get_completed, ToolResult):
        return get_completed

    existing_item: Mapping[str, object] | None = None
    item_id: str | None = None
    if get_completed.returncode == 0:
        try:
            parsed = json.loads(get_completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(
                status="error", error=f"bw get item returned non-JSON output: {exc}"
            )
        if not isinstance(parsed, Mapping):
            return ToolResult(
                status="error", error="bw get item output was not a JSON object."
            )
        existing_item = parsed
        item_id = str(parsed.get("id") or "").strip() or None

    try:
        payload = _bitwarden_item_payload(
            name=item_name, value=value, existing_item=existing_item
        )
    except ValueError as exc:
        return ToolResult(
            status="error",
            error=f"bw get item returned an invalid item structure: {exc}",
        )
    encoded = _bitwarden_encode_item(executable, payload)
    if isinstance(encoded, ToolResult):
        return encoded
    return encoded, item_id


def _write_bitwarden_state(
    config: CredentialStateBackendConfig, key: str, value: str
) -> ToolResult:
    executable = shutil.which("bw")
    if not executable:
        return ToolResult(
            status="error", error="Required CLI is not installed or not on PATH: bw"
        )
    config_result = ensure_bitwarden_server_configured_from_env(
        credential_owner_environment()
    )
    if config_result is not None:
        return config_result
    item_name = credential_state_item_name(config, key)
    prepared = _prepare_bitwarden_write(
        executable=executable, item_name=item_name, value=value
    )
    if isinstance(prepared, ToolResult):
        return prepared
    encoded, item_id = prepared
    if item_id:
        command = [executable, "edit", "item", item_id]
        action = "edit.item"
    else:
        command = [executable, "create", "item"]
        action = "create.item"
    completed = _run_bitwarden(command, input_text=encoded, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = {
        "credential_state_backend": "credential_source",
        "credential_state_source": "bitwarden",
        "credential_state_key": key,
        "credential_state_item_name": item_name,
        "action": action,
        "exit_code": completed.returncode,
        "command": command,
    }
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            content=content,
            error=f"bw {action.replace('.', ' ')} exited with code {completed.returncode}",
        )
    return ToolResult(status="success", content=content)


def _write_sealbox_state(
    config: CredentialStateBackendConfig, key: str, value: str
) -> ToolResult:
    item_key = credential_state_sealbox_key(config, key)
    command = ["secret", "set", item_key, "--output", "json"]
    owner_id = active_credential_owner_id()
    tenant_env = None
    if owner_id:
        tenant_env = sealbox_tenant_environment(
            scope_kind=active_credential_owner_scope_kind(),
            owner_id=owner_id,
            provision=True,
        )
        if isinstance(tenant_env, ToolResult):
            return tenant_env
    completed = run_sealbox(
        command, input_text=value, env=tenant_env, timeout_seconds=30
    )
    if isinstance(completed, ToolResult):
        return completed
    content = sealbox_redacted_completed_content(
        completed,
        action="credential_state.secret.set",
        command=["sealbox-cli", *command],
        include_stdout=False,
    )
    content.update(
        {
            "credential_state_backend": "credential_source",
            "credential_state_source": "sealbox",
            "credential_state_key": key,
            "credential_state_item_key": item_key,
        }
    )
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            content=content,
            error=f"sealbox-cli secret set exited with code {completed.returncode}",
        )
    return ToolResult(status="success", content=content)


def _write_credential_source_state(
    config: CredentialStateBackendConfig, key: str, value: str
) -> ToolResult:
    source_name = normalize_credential_source_name(config.source or "")
    if source_name == "vault":
        return _write_vault_state(config, key, value)
    if source_name == "bitwarden":
        return _write_bitwarden_state(config, key, value)
    if source_name == "sealbox":
        return _write_sealbox_state(config, key, value)
    return ToolResult(
        status="error",
        error=(
            f"Credential-state writes are not implemented safely for {source_name or 'the configured source'}. "
            "Use source=sealbox, source=vault, source=bitwarden, or mode=files."
        ),
        content={
            "credential_state_backend": "credential_source",
            "credential_state_source": source_name or None,
            "credential_state_key": key,
        },
    )


def credential_state_backend_config(
    env: Mapping[str, str] | None = None,
) -> CredentialStateBackendConfig:
    return CredentialStateBackendConfig.from_env(env)


def credential_state_backend_mode(env: Mapping[str, str] | None = None) -> str:
    return credential_state_backend_config(env).mode


def read_credential_state(
    key: str,
    *,
    env: Mapping[str, str] | None = None,
    owner_id: str | None = None,
) -> str | ToolResult | None:
    if owner_id and owner_id != active_credential_owner_id():
        with credential_owner_context(owner_id):
            return read_credential_state(key, env=env)
    _state_key_path(key)
    config = credential_state_backend_config(env)
    if config.mode == "files":
        return _read_file_state(key)
    if config.mode != "credential_source":
        return ToolResult(
            status="error",
            error=f"Unsupported credential-state backend mode: {config.mode}",
        )
    result = _read_credential_source_state(config, key)
    if isinstance(result, ToolResult) and normalize_credential_source_name(config.source or "") == _SEALBOX:
        # A file copy cannot repair tenant authentication or encryption material.
        return result
    if isinstance(result, ToolResult) and config.fallback_to_files:
        fallback = _read_file_state(key)
        return fallback if fallback is not None else result
    if result is None and config.fallback_to_files:
        return _read_file_state(key)
    return result


def write_credential_state(
    key: str,
    value: str,
    *,
    env: Mapping[str, str] | None = None,
    owner_id: str | None = None,
    exclusive: bool = False,
) -> ToolResult:
    if owner_id and owner_id != active_credential_owner_id():
        with credential_owner_context(owner_id):
            return write_credential_state(key, value, env=env, exclusive=exclusive)
    _state_key_path(key)
    config = credential_state_backend_config(env)
    if config.mode == "files":
        return _write_file_state(key, value, exclusive=exclusive)
    if config.mode != "credential_source":
        return ToolResult(
            status="error",
            error=f"Unsupported credential-state backend mode: {config.mode}",
        )
    result = _write_credential_source_state(config, key, value)
    if (
        result.status != "success"
        and config.fallback_to_files
        and normalize_credential_source_name(config.source or "") != _SEALBOX
    ):
        return _write_file_state(key, value, exclusive=exclusive)
    return result


# -- Deleting a person's stored values ------------------------------------------------
#
# Used when a person is removed from the team
# (``secure_tool_broker.departed_person_credentials``). Each backend deletes
# the way its write stores: the file below the person's credentials folder,
# the Sealbox secret in the person's tenant, the Vault path with every
# version, the Bitwarden item for good. A value that is not there is not an
# error, so a repeated removal is harmless. Failures are ToolResult errors
# whose content never holds a value.


def _state_deleted(key: str, backend: str, *, found: bool, **details: object) -> ToolResult:
    return ToolResult(
        status="success",
        content={"credential_state_backend": backend, "credential_state_key": key, STATE_FOUND: found, **details},
    )


def _delete_file_state(key: str, env: Mapping[str, str] | None) -> ToolResult:
    """Delete the person's file for ``key`` without following links (``safe_open.remove_file``)."""

    root = broker_credentials_root(env)
    owner_id = str(active_credential_owner_id() or "")
    if root is None or not root.is_dir():
        return _state_deleted(key, _FILES, found=False)
    try:
        safe_open.remove_file(root, person_credential_parts(owner_id, *_state_key_path(key)))
    except safe_open.UnsafePath as exc:
        if exc.reason == safe_open.REASON_NOT_FOUND:
            return _state_deleted(key, _FILES, found=False)
        if exc.reason == safe_open.REASON_LINK:
            # The broker writes regular files only, so a link is not the person's
            # value and is not followed; removing their folder deletes the link
            # itself, or reports it when it stands in place of the folder.
            logger.warning(
                "A link stands where a person's stored sign-in file belongs; it is not followed. "
                "event=credential_state_file_link_skipped person_id=%s credential_state_key=%s",
                owner_id,
                key,
            )
            return _state_deleted(key, _FILES, found=False)
        return ToolResult(
            status="error",
            error=f"The stored file for {key} could not be deleted safely ({exc.reason}).",
            content={"credential_state_backend": _FILES, "credential_state_key": key, "reason": exc.reason},
        )
    return _state_deleted(key, _FILES, found=True)


def _delete_sealbox_state(config: CredentialStateBackendConfig, key: str) -> ToolResult:
    item_key = credential_state_sealbox_key(config, key)
    tenant_env = sealbox_tenant_environment(
        scope_kind=active_credential_owner_scope_kind(),
        owner_id=active_credential_owner_id(),
        provision=False,
    )
    if isinstance(tenant_env, ToolResult):
        if tenant_env.content.get("error_code") == SEALBOX_TENANT_NOT_PROVISIONED:
            return _state_deleted(key, _SEALBOX, found=False, credential_state_item_key=item_key)
        return tenant_env
    deleted = delete_sealbox_item(item_key=item_key, item_kind="secret", env=tenant_env)
    if isinstance(deleted, ToolResult):
        return deleted
    return _state_deleted(
        key, _SEALBOX, found=not deleted.get("already_absent"), credential_state_item_key=item_key
    )


def _vault_failure(completed: subprocess.CompletedProcess[str], *, key: str, action: str) -> ToolResult:
    content: dict[str, object] = {
        "credential_state_backend": _VAULT,
        "credential_state_key": key,
        "exit_code": completed.returncode,
    }
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    return ToolResult(
        status="error", content=content, error=f"vault {action} exited with code {completed.returncode}"
    )


def _delete_vault_state(config: CredentialStateBackendConfig, key: str) -> ToolResult:
    """Delete the path with every version (``kv metadata delete``); the path must be on a KV v2 mount."""

    executable = shutil.which("vault")
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: vault")
    mount = f"-mount={_vault_default_mount()}"
    path = credential_state_item_path(config, key)
    # ``metadata get`` says whether the path exists without printing any value.
    probe = _run_vault([executable, "kv", "metadata", "get", mount, path], action="kv metadata get")
    if isinstance(probe, ToolResult):
        return probe
    if probe.returncode != 0:
        if "no value found" in (probe.stderr or "").lower():
            return _state_deleted(key, _VAULT, found=False, credential_state_path=path)
        return _vault_failure(probe, key=key, action="kv metadata get")
    deleted = _run_vault([executable, "kv", "metadata", "delete", mount, path], action="kv metadata delete")
    if isinstance(deleted, ToolResult):
        return deleted
    if deleted.returncode != 0:
        return _vault_failure(deleted, key=key, action="kv metadata delete")
    return _state_deleted(key, _VAULT, found=True, credential_state_path=path)


def _bitwarden_item_id(executable: str, item_name: str, key: str) -> str | None | ToolResult:
    """The id of the named item, None when there is none, or the failure."""

    found = _run_bitwarden([executable, "get", "item", item_name], timeout_seconds=_TIMEOUT_SECONDS)
    if isinstance(found, ToolResult):
        return found
    if found.returncode != 0:
        stderr = redact_text(found.stderr or "").strip()
        if "not found" in stderr.lower():
            return None
        return ToolResult(
            status="error",
            error=f"bw get item exited with code {found.returncode}",
            content={"credential_state_backend": _BITWARDEN, "credential_state_key": key, "stderr": stderr},
        )
    try:
        parsed = json.loads(found.stdout or "{}")
    except json.JSONDecodeError:
        parsed = None
    item_id = str(parsed.get("id") or "").strip() if isinstance(parsed, Mapping) else ""
    if not item_id:
        return ToolResult(status="error", error="bw get item did not return an item id.")
    return item_id


def _delete_bitwarden_state(config: CredentialStateBackendConfig, key: str) -> ToolResult:
    executable = shutil.which("bw")
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
    config_result = ensure_bitwarden_server_configured_from_env(credential_owner_environment())
    if config_result is not None:
        return config_result
    item_name = credential_state_item_name(config, key)
    item_id = _bitwarden_item_id(executable, item_name, key)
    if isinstance(item_id, ToolResult):
        return item_id
    if item_id is None:
        return _state_deleted(key, _BITWARDEN, found=False, credential_state_item_name=item_name)
    command = [executable, "delete", "item", item_id, "--permanent"]
    completed = _run_bitwarden(command, timeout_seconds=_TIMEOUT_SECONDS)
    if isinstance(completed, ToolResult):
        return completed
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            error=f"bw delete item exited with code {completed.returncode}",
            content={
                "credential_state_backend": _BITWARDEN,
                "credential_state_key": key,
                "stderr": redact_text(completed.stderr or "").strip(),
            },
        )
    return _state_deleted(key, _BITWARDEN, found=True, credential_state_item_name=item_name)


def _delete_credential_source_state(config: CredentialStateBackendConfig, key: str) -> ToolResult:
    source_name = normalize_credential_source_name(config.source or "")
    if source_name == _VAULT:
        return _delete_vault_state(config, key)
    if source_name == _BITWARDEN:
        return _delete_bitwarden_state(config, key)
    if source_name == _SEALBOX:
        return _delete_sealbox_state(config, key)
    # The broker never writes credential state to other sources
    # (``_write_credential_source_state``), so there is nothing of its own there.
    return _state_deleted(key, _CREDENTIAL_SOURCE, found=False, credential_state_source=source_name or None)


def _backend_deletion(config: CredentialStateBackendConfig, key: str) -> ToolResult | None:
    """The configured backend's deletion; None in file mode, where the file is the store."""

    if config.mode == _FILES:
        return None
    if config.mode != _CREDENTIAL_SOURCE:
        return ToolResult(status="error", error=f"Unsupported credential-state backend mode: {config.mode}")
    return _delete_credential_source_state(config, key)


def delete_credential_state(
    key: str,
    *,
    owner_id: str,
    env: Mapping[str, str] | None = None,
) -> ToolResult:
    """Delete a person's stored value for ``key``; success with ``STATE_FOUND`` saying whether one was there.

    The configured backend is deleted from, and the person's file for the key
    always as well: a file is the store in file mode, the fallback copy with
    ``fallback_to_files``, or a leftover from an earlier file-mode setup.
    Only the person's own scope is named, so the team's values are never
    touched. Idempotent; a failure is logged with identifiers only.
    """

    _state_key_path(key)
    with credential_owner_context(owner_id):
        config = credential_state_backend_config(env)
        results = [_delete_file_state(key, env)]
        backend = _backend_deletion(config, key)
        if backend is not None:
            results.insert(0, backend)
    failure = next((result for result in results if result.status != "success"), None)
    if failure is not None:
        logger.error(
            "Could not delete a person's stored sign-in. event=credential_state_delete_failed "
            "person_id=%s credential_state_key=%s mode=%s source=%s error=%s",
            owner_id,
            key,
            config.mode,
            config.source,
            redact_text(str(failure.error or "")),
        )
        return failure
    found = any(result.content.get(STATE_FOUND) for result in results)
    return _state_deleted(
        key,
        config.mode,
        found=found,
        deleted_from=[result.content.get("credential_state_backend") for result in results if result.content.get(STATE_FOUND)],
    )
