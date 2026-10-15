"""Provision and resolve generic Sealbox tenants for broker owner scopes.

This module is a Sealbox client. It depends only on the public Sealbox CLI
contract and keeps UA identity-to-tenant mapping entirely on the UA side.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
    SealboxTenantMapping,
    resolve_catalog_db_path,
)
from secure_tool_broker.sealbox_cli import (
    chmod_private_file,
    first_json_value,
    redacted_completed_content,
    run_sealbox,
)
from secure_tool_broker.sealbox_runtime import ensure_sealbox_key_pair_files
from secure_tools.providers import ToolResult


logger = logging.getLogger(__name__)
SEALBOX_TENANT_NOT_PROVISIONED = "sealbox_tenant_not_provisioned"


def _catalog_store() -> CredentialCatalogStore:
    return CredentialCatalogStore(resolve_catalog_db_path())


def _material_root() -> Path:
    explicit = str(os.getenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    credentials_dir = str(os.getenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR") or "").strip()
    if credentials_dir:
        return (Path(credentials_dir).expanduser() / "sealbox" / "tenants").resolve()
    return (resolve_catalog_db_path().parent / "broker_credentials" / "sealbox" / "tenants").resolve()


def _root_token_file() -> Path | None:
    raw = str(os.getenv("SEALBOX_ROOT_TOKEN_FILE") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _material_id(scope: CredentialOwnerScope) -> str:
    digest = hashlib.sha256(f"{scope.scope_kind}\0{scope.owner_id}".encode("utf-8")).hexdigest()
    return f"owner-{digest[:32]}"


def _base_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["SEALBOX_API_VERSION"] = "v2"
    env["SEALBOX_OUTPUT_FORMAT"] = "json"
    return env


def _root_environment() -> dict[str, str] | ToolResult:
    token_file = _root_token_file()
    url = str(os.getenv("SEALBOX_URL") or "").strip()
    if not url or token_file is None:
        return ToolResult(
            status="error",
            error="Sealbox root administration is not configured for tenant provisioning.",
            content={
                "error_code": "sealbox_root_not_configured",
                "required": ["SEALBOX_URL", "SEALBOX_ROOT_TOKEN_FILE"],
            },
        )
    if not token_file.is_file():
        return ToolResult(
            status="error",
            error=f"Sealbox root token file does not exist: {token_file}",
            content={"error_code": "sealbox_root_token_missing"},
        )
    env = _base_environment()
    for key in (
        "SEALBOX_TOKEN",
        "SEALBOX_PUBLIC_KEY",
        "SEALBOX_PUBLIC_KEY_FILE",
        "SEALBOX_PRIVATE_KEY",
        "SEALBOX_PRIVATE_KEY_FILE",
    ):
        env.pop(key, None)
    env["SEALBOX_URL"] = url
    env["SEALBOX_TOKEN_FILE"] = str(token_file)
    return env


def _tenant_environment(mapping: SealboxTenantMapping) -> dict[str, str] | ToolResult:
    directory = _material_root() / mapping.material_id
    token_file = directory / "token"
    public_key_file = directory / "public.pem"
    private_key_file = directory / "private.pem"
    missing = [
        str(path)
        for path in (token_file, public_key_file, private_key_file)
        if not path.is_file()
    ]
    if missing:
        return ToolResult(
            status="error",
            error="Sealbox tenant mapping exists but required client key material is missing.",
            content={
                "error_code": "sealbox_tenant_material_missing",
                "tenant_id": mapping.tenant_id,
                "missing_files": missing,
            },
        )
    invalid_material = _validate_tenant_keys(mapping, public_key_file, private_key_file)
    if invalid_material is not None:
        return invalid_material
    env = _base_environment()
    for key in (
        "SEALBOX_TOKEN",
        "SEALBOX_ROOT_TOKEN_FILE",
        "SEALBOX_AUTH_TOKEN_FILE",
        "AUTH_TOKEN_FILE",
    ):
        env.pop(key, None)
    env["SEALBOX_URL"] = str(os.getenv("SEALBOX_URL") or "").strip()
    env["SEALBOX_TOKEN_FILE"] = str(token_file)
    env["SEALBOX_PUBLIC_KEY_FILE"] = str(public_key_file)
    env["SEALBOX_PRIVATE_KEY_FILE"] = str(private_key_file)
    return env


def _validate_tenant_keys(
    mapping: SealboxTenantMapping, public_key_file: Path, private_key_file: Path
) -> ToolResult | None:
    """Check both halves before any read or write; never repair an existing tenant.

    Sealbox reads use only the private key and writes only the public key.
    Validating the pair here prevents an operation from succeeding while the
    other half is corrupt, or encrypting new records to a mismatched key.
    """

    try:
        public_key = serialization.load_pem_public_key(public_key_file.read_bytes())
        private_key = serialization.load_pem_private_key(private_key_file.read_bytes(), password=None)
        if not isinstance(public_key, rsa.RSAPublicKey) or not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Tenant keys must be RSA keys")
        if public_key.public_numbers() != private_key.public_key().public_numbers():
            raise ValueError("Tenant key pair does not match")
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        return ToolResult(
            status="error",
            error="Sealbox tenant client key material is unreadable, invalid or mismatched. Restore its original key files from a backup.",
            content={"error_code": "sealbox_tenant_material_invalid", "tenant_id": mapping.tenant_id},
        )
    return None


@contextmanager
def _provisioning_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    lock_path = directory / ".provision.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


def _run_checked(
    args: list[str],
    *,
    env: Mapping[str, str],
    action: str,
) -> Mapping[str, object] | ToolResult:
    completed = run_sealbox(args, env=env, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = redacted_completed_content(
        completed,
        action=action,
        command=["sealbox-cli", *args],
    )
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            error=f"sealbox-cli {' '.join(args[:2])} exited with code {completed.returncode}",
            content=content,
        )
    parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, Mapping))
    if not isinstance(parsed, Mapping):
        return ToolResult(
            status="error",
            error=f"Sealbox {action} did not return JSON metadata.",
            content=content,
        )
    return parsed


def _persist_provisioning_metadata(path: Path, *, tenant_id: str) -> None:
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps({"tenant_id": tenant_id}, sort_keys=True), encoding="utf-8")
    chmod_private_file(temp_path)
    temp_path.replace(path)
    chmod_private_file(path)


def _read_provisioning_metadata(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    tenant_id = str(payload.get("tenant_id") or "").strip() if isinstance(payload, Mapping) else ""
    return tenant_id or None


def _provision(scope: CredentialOwnerScope, store: CredentialCatalogStore) -> dict[str, str] | ToolResult:
    material_id = _material_id(scope)
    directory = _material_root() / material_id
    existing = store.get_sealbox_tenant_mapping(scope)
    if existing is not None:
        return _tenant_environment(existing)
    root_env = _root_environment()
    if isinstance(root_env, ToolResult):
        return root_env
    with _provisioning_lock(directory):
        existing = store.get_sealbox_tenant_mapping(scope)
        if existing is not None:
            return _tenant_environment(existing)

        token_file = directory / "token"
        public_key_file = directory / "public.pem"
        private_key_file = directory / "private.pem"
        metadata_file = directory / "tenant.json"
        tenant_id = _read_provisioning_metadata(metadata_file)

        if tenant_id is None:
            partial = [path for path in (token_file, metadata_file) if path.exists()]
            if partial:
                return ToolResult(
                    status="error",
                    error="Incomplete Sealbox tenant provisioning state requires operator repair.",
                    content={
                        "error_code": "sealbox_tenant_provisioning_incomplete",
                        "material_id": material_id,
                    },
                )
            ensure_sealbox_key_pair_files(
                public_key_file=public_key_file,
                private_key_file=private_key_file,
            )
            created = _run_checked(
                [
                    "tenant",
                    "create",
                    "--display-name",
                    f"Credential owner {material_id}",
                    "--token-label",
                    "ua-broker",
                    "--token-file",
                    str(token_file),
                ],
                env=root_env,
                action="tenant.create",
            )
            if isinstance(created, ToolResult):
                return created
            tenant = created.get("tenant")
            tenant_id = str(tenant.get("id") or "").strip() if isinstance(tenant, Mapping) else ""
            if not tenant_id:
                return ToolResult(
                    status="error",
                    error="Sealbox tenant create response did not include tenant.id.",
                    content={"error_code": "sealbox_tenant_id_missing"},
                )
            _persist_provisioning_metadata(metadata_file, tenant_id=tenant_id)
        else:
            ensure_sealbox_key_pair_files(
                public_key_file=public_key_file,
                private_key_file=private_key_file,
            )

        candidate = SealboxTenantMapping(
            scope_kind=scope.scope_kind,
            owner_id=scope.owner_id,
            tenant_id=tenant_id,
            material_id=material_id,
            created_at="",
        )
        tenant_env = _tenant_environment(candidate)
        if isinstance(tenant_env, ToolResult):
            return tenant_env
        registered = _run_checked(
            ["key", "register", "--output", "json"],
            env=tenant_env,
            action="key.register",
        )
        if isinstance(registered, ToolResult):
            # A retry after a crash may encounter an already-registered key.
            status = run_sealbox(["key", "status", "--output", "json"], env=tenant_env, timeout_seconds=15)
            if isinstance(status, ToolResult) or status.returncode != 0:
                return registered
        mapping = store.put_sealbox_tenant_mapping(
            scope,
            tenant_id=tenant_id,
            material_id=material_id,
        )
        logger.info(
            "Provisioned Sealbox tenant mapping. event=sealbox_tenant_provisioned scope_kind=%s owner_id=%s tenant_id=%s",
            scope.scope_kind,
            scope.owner_id,
            mapping.tenant_id,
        )
        return _tenant_environment(mapping)


def sealbox_tenant_environment(
    *,
    scope_kind: str | None,
    owner_id: str | None,
    provision: bool,
) -> dict[str, str] | ToolResult:
    try:
        scope = CredentialOwnerScope(scope_kind or "user", owner_id or "")
    except ValueError as exc:
        return ToolResult(
            status="error",
            error=f"Sealbox credential access requires a valid owner scope: {exc}",
            content={"error_code": "credential_owner_required"},
        )
    store = _catalog_store()
    mapping = store.get_sealbox_tenant_mapping(scope)
    if mapping is not None:
        return _tenant_environment(mapping)
    if not provision:
        return ToolResult(
            status="error",
            error="No Sealbox tenant has been provisioned for this credential owner.",
            content={"error_code": SEALBOX_TENANT_NOT_PROVISIONED},
        )
    return _provision(scope, store)


__all__ = ["sealbox_tenant_environment"]
