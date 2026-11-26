"""Provider-neutral credential catalog and OpenShell binding helpers."""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from pydantic import BaseModel, Field, field_validator

from custom_messages.contracts.access_v2 import (
    REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED,
    REASON_ACCOUNT_LINK_MISSING,
    AccountLinkV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
)
from env_secrets import get_env_with_file_fallback
from secure_tool_broker.bitwarden_cli import ensure_bitwarden_server_configured_from_env
from secure_tool_broker.credential_diagnostics import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_FIELD_MISSING,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_SOURCE_FAILED,
    CREDENTIAL_SOURCES_UNCONFIGURED,
    CREDENTIAL_VALUE_UNAVAILABLE,
)
from secure_tool_broker.credential_expiry import (
    CREDENTIAL_DROPPED_CLEANUP_PENDING,
    CREDENTIAL_EXPIRY_FORM_ONLY,
    CREDENTIAL_EXPIRY_INVALID,
    CREDENTIAL_EXPIRY_SHARED_RECORD,
    CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE,
    CREDENTIAL_REPLACEMENT_FORM_ONLY,
    CredentialExpiryError,
    format_utc_timestamp,
    is_expired,
    parse_utc_timestamp,
    remaining_ttl_seconds,
    validate_storage_expiry,
)
from secure_tool_broker.credential_sources import (
    CredentialRef,
    CredentialSecretRequest,
    get_credential_source,
    normalize_credential_source_name,
)
from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
    PendingDroppedSealboxDelete,
    load_credential_readonly,
    load_scope_readonly,
    resolve_catalog_db_path,
)
from secure_tool_broker.credential_state import (
    active_credential_owner_id,
    credential_owner_context,
    credential_owner_environment,
)
from secure_tool_broker.credential_templates import (
    get_credential_template,
    list_credential_templates,
    render_credential_template_request,
    save_approved_template_variant,
)
from secure_tool_broker.defaults import DEFAULT_API_REQUEST_USER_AGENT
from secure_tool_broker.redaction import SECRET_VALUE_RE, redact_json, redact_text
from secure_tool_broker.sealbox_cli import (
    first_json_value,
    is_auth_required as sealbox_is_auth_required,
    redacted_completed_content as sealbox_redacted_completed_content,
    reject_unsupported_key as sealbox_reject_unsupported_key,
    run_sealbox,
    safe_key_component,
)
from secure_tool_broker.sealbox_tenants import SEALBOX_TENANT_NOT_PROVISIONED, sealbox_tenant_environment
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction
from runtime_settings.openshell_naming import ENV_SANDBOX_NAME as OPENSHELL_ENV_SANDBOX_NAME
from runtime_settings.openshell_naming import sandbox_name as openshell_sandbox_name


_LOGGER = logging.getLogger(__name__)
CATALOG_SCHEMA_VERSION = 1
DEFAULT_PASSWORD_LENGTH = 32
SIGNUP_BOUNDARY_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "bot challenge",
    "bot detection",
    "webauthn",
    "passkey",
    "mfa",
    "multi-factor",
    "sms code",
    "email code",
    "payment",
    "identity verification",
    "manual verification",
)
SECRET_FIELD_KINDS = {"password", "token", "api_key", "secret", "totp", "credential_file"}
METADATA_FIELD_KINDS = {"username", "email", "login", "account_id", "service_label"}
_LEASES: dict[str, dict[str, Any]] = {}
_SECRET_CACHE: dict[
    tuple[str, str, str, str | None, str | None, str | None, str | None, str],
    tuple[str, float, datetime | None],
] = {}
# Per-(scope_kind, owner_id, credential_key) lock serializing writes and janitor
# deletions in-process so a renewal cannot interleave with an expiry sweep.
_KEY_LOCKS_GUARD = threading.Lock()
_KEY_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
# Leaf per-(scope_kind, owner_id) lock serializing cross-key Sealbox alias
# checks and catalog saves; never held across subprocess calls.
_SCOPE_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def credential_key_lock(scope: CredentialOwnerScope, credential_key: str) -> threading.Lock:
    """Return the in-process lock for one owner's catalog key."""

    lock_key = (scope.scope_kind, scope.owner_id, _credential_key(credential_key))
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[lock_key] = lock
        return lock


def _scope_catalog_lock(scope: CredentialOwnerScope) -> threading.Lock:
    """Return the leaf lock serializing cross-key checks and saves for one owner."""

    lock_key = (scope.scope_kind, scope.owner_id)
    with _KEY_LOCKS_GUARD:
        lock = _SCOPE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _SCOPE_LOCKS[lock_key] = lock
        return lock
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}"
)
_ALLOWED_API_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
OPENSHELL_DEFAULT_PROVIDER_TYPE = "generic"
_OPENSHELL_PROVIDER_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Error codes of credential.bind_for_openshell (a missing link reports the
# shared REASON_ACCOUNT_LINK_MISSING from the access contracts).
ACCOUNT_LINK_TOOL_MISMATCH = "account_link_tool_mismatch"
ACCOUNT_LINK_HAS_NO_ENVIRONMENT = "account_link_has_no_environment"
OPENSHELL_TOOL_ID_REQUIRED = "openshell_tool_id_required"
OPENSHELL_PROVIDER_TYPE_INVALID = "openshell_provider_type_invalid"
OPENSHELL_SANDBOX_NOT_OURS = "openshell_sandbox_not_the_persons"


class CatalogSourceRef(BaseModel):
    source: str = Field(description="Credential source type: env, sealbox, bitwarden, vault, onepassword, or keepass.")
    env_var: str | None = None
    file_fallback: bool = True
    item_id: str | None = None
    item_name: str | None = None
    field_ref: str = "password"
    vault: str | None = None
    folder: str | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> str:
        source = str(value or "").strip().lower()
        return normalize_credential_source_name(source) if source != "env" else source


class CatalogCredentialField(BaseModel):
    kind: str = "custom"
    visibility: str = "secret"
    value_source: str = "existing_source"
    value: str | None = None
    generation_policy: str | None = None
    resolution_order: list[CatalogSourceRef] = Field(default_factory=list)

    @field_validator("kind", "visibility", "value_source", "generation_policy", mode="before")
    @classmethod
    def _normalize_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip().lower()

    def is_secret(self) -> bool:
        return self.visibility == "secret" or self.kind in SECRET_FIELD_KINDS


class CatalogCredential(BaseModel):
    """A saved credential: its fields and where each secret is stored.

    Which tool uses a credential, and where its values go (a header, a query
    value, or an environment variable), is an account link
    (``AccountLinkV1``), not part of the credential. Credentials saved before
    account links may still carry a ``bindings`` key; the default pydantic
    config ignores it, and the next save drops it.
    """

    display_name: str
    purpose: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str | None = None
    approval: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, CatalogCredentialField] = Field(default_factory=dict)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _validate_expires_at(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return format_utc_timestamp(value)


class CredentialCatalog(BaseModel):
    schema_version: int = CATALOG_SCHEMA_VERSION
    credentials: dict[str, CatalogCredential] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if int(value) != CATALOG_SCHEMA_VERSION:
            raise ValueError(f"Unsupported credential catalog schema_version: {value}")
        return int(value)


@dataclass(frozen=True)
class ResolvedCredentialField:
    field_name: str
    value: str
    source: str
    source_ref: dict[str, Any]
    secret: bool


@dataclass(frozen=True)
class CredentialWriteResult:
    source: str
    source_ref_by_field: dict[str, CatalogSourceRef]
    item_id: str | None = None
    item_name: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_openshell_binding_ttl_seconds() -> int:
    raw = str(os.getenv("SECURE_TOOL_BROKER_OPENSHELL_BINDING_TTL_SECONDS") or "900").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 900


def _default_secret_cache_ttl_seconds() -> int:
    raw = str(os.getenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS") or "300").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 300


def _credential_resolution_env_layers(env: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    base_env = dict(env if env is not None else os.environ)
    team_env_values = _load_team_env_values(base_env)
    return team_env_values, base_env


def _resolve_env_value_with_team_priority(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    file_fallback: bool = True,
) -> str | None:
    team_env_values, base_env = _credential_resolution_env_layers(env)
    for env_mapping in (team_env_values, base_env):
        if not env_mapping:
            continue
        if file_fallback:
            value = get_env_with_file_fallback(name, env=env_mapping)
        else:
            value = _string(env_mapping.get(name)) or None
        if value:
            return value
    return None


def _load_team_env_values(env: Mapping[str, str]) -> dict[str, str]:
    path = _resolve_team_env_path(env)
    if path is None or not path.exists() or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY_PATTERN.fullmatch(key):
            continue
        values[key] = _parse_env_file_value(raw_value)
    return values


def _resolve_team_env_path(env: Mapping[str, str]) -> Path | None:
    explicit = _string(env.get("SECURE_TOOL_BROKER_TEAM_ENV_PATH") or env.get("TEAM_ENV_PATH"))
    if explicit:
        return Path(explicit).expanduser().resolve()
    instance_root = _string(env.get("TEAM_INSTANCE_ROOT"))
    if instance_root:
        return (Path(instance_root).expanduser() / "team.env").resolve()
    return None


def _parse_env_file_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
        if raw_value.strip().startswith('"'):
            value = value.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
    return value


def load_catalog(*, scope: CredentialOwnerScope) -> CredentialCatalog:
    """One owner's catalog rows from the SQLite catalog."""

    store = CredentialCatalogStore(resolve_catalog_db_path())
    return CredentialCatalog(credentials=store.load_scope(scope))


def read_credential_metadata_readonly(
    credential_key: str,
    *,
    scope: CredentialOwnerScope,
    fallback_db_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Read sanitized non-secret metadata for one credential without creating or modifying the DB.

    Resolves the catalog path through ``resolve_catalog_db_path()`` whenever
    ``SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH`` is set (matching the
    broker process), falling back to ``fallback_db_path`` only when the env
    override is absent. Never returns secret values or secret source locations,
    and treats temporary credentials past ``expires_at`` as absent.
    """

    key = _credential_key(credential_key)
    explicit = str(os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH") or "").strip()
    if explicit or fallback_db_path is None:
        db_path = resolve_catalog_db_path()
    else:
        db_path = Path(fallback_db_path).expanduser().resolve()
    raw = load_credential_readonly(db_path, scope, key)
    if raw is None:
        return None
    credential = CatalogCredential.model_validate(raw)
    if is_expired(credential.expires_at, now=now):
        return None
    return _metadata_for_agent(key, credential)


def save_catalog(catalog: CredentialCatalog, *, scope: CredentialOwnerScope) -> Path:
    """Replace one owner's catalog rows; other owners' rows are untouched."""

    store = CredentialCatalogStore(resolve_catalog_db_path())
    store.replace_scope(
        scope,
        {
            key: credential.model_dump(mode="json", exclude_none=True)
            for key, credential in catalog.credentials.items()
        },
    )
    return store.path


def _save_catalog_credential(
    credential_key: str,
    credential: CatalogCredential,
    *,
    scope: CredentialOwnerScope,
    dropped_sealbox_targets: Sequence[tuple[str, str]] = (),
    queued_by: str = "form",
) -> Path:
    store = CredentialCatalogStore(resolve_catalog_db_path())
    payload = credential.model_dump(mode="json", exclude_none=True)
    store.put_credential(
        scope,
        credential_key,
        payload,
        dropped_sealbox_targets=dropped_sealbox_targets,
        queued_by=queued_by,
    )
    return store.path


def _delete_catalog_credential(
    credential_key: str,
    *,
    scope: CredentialOwnerScope,
) -> bool:
    store = CredentialCatalogStore(resolve_catalog_db_path())
    return store.delete_credential(scope, credential_key)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _credential_key(value: Any) -> str:
    key = _string(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ValueError("credential_key must contain only letters, numbers, underscore, dot, colon, or dash.")
    return key


def _owner_scope(
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
) -> CredentialOwnerScope:
    """The owner a catalog call acts for: the explicit one, else the signed context's person.

    There is no default owner; a call that names neither is refused.
    """
    if scope is not None:
        return scope
    if context is not None:
        return CredentialOwnerScope.user(context.require_user())
    raise ValueError("A credential catalog call must name its owner (a person or the team).")


def _redacted_source_ref(source_ref: CatalogSourceRef) -> dict[str, Any]:
    data = source_ref.model_dump(mode="json", exclude_none=True)
    return {key: value for key, value in data.items() if key not in {"value", "password", "token", "secret"}}


def _metadata_for_agent(credential_key: str, credential: CatalogCredential) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    for field_name, field in credential.fields.items():
        payload = {
            "kind": field.kind,
            "visibility": field.visibility,
            "value_source": field.value_source,
            "secret": field.is_secret(),
            "resolution_sources": [source.source for source in field.resolution_order],
        }
        if not field.is_secret() and field.value is not None:
            payload["value"] = field.value
        fields[field_name] = payload
    metadata: dict[str, Any] = {
        "credential_key": credential_key,
        "display_name": credential.display_name,
        "purpose": credential.purpose,
        "fields": fields,
        "approval": {
            key: value
            for key, value in credential.approval.items()
            if key not in {"token", "password", "secret", "credential"}
        },
    }
    if credential.expires_at is not None:
        metadata["expires_at"] = credential.expires_at
    return metadata


def _evict_credential_secret_cache(
    credential: CatalogCredential,
    scope: CredentialOwnerScope,
) -> None:
    """Remove any cached secret entries referenced by ``credential`` in ``scope``."""

    for field in credential.fields.values():
        for source_ref in field.resolution_order:
            _SECRET_CACHE.pop(_secret_cache_key(source_ref, scope), None)


def resolve_secret_field(
    credential_key: str,
    field_name: str,
    *,
    catalog: CredentialCatalog | None = None,
    env: Mapping[str, str] | None = None,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
    now: datetime | None = None,
) -> ResolvedCredentialField | ToolResult:
    key = _credential_key(credential_key)
    resolved_scope = _owner_scope(context=context, scope=scope)
    active_catalog = catalog if catalog is not None else load_catalog(scope=resolved_scope)
    credential = active_catalog.credentials.get(key)
    if credential is None:
        return ToolResult(
            status="error",
            error=f"Credential key is not registered: {key}",
            content={"error_code": CREDENTIAL_NOT_FOUND, "credential_key": key},
        )
    if is_expired(credential.expires_at, now=now):
        _evict_credential_secret_cache(credential, resolved_scope)
        return ToolResult(
            status="error",
            error=f"Temporary credential has expired: {key}",
            content={
                "error_code": CREDENTIAL_EXPIRED,
                "credential_key": key,
                "field_ref": field_name,
                "expires_at": credential.expires_at,
            },
        )
    field = credential.fields.get(field_name)
    if field is None:
        return ToolResult(
            status="error",
            error=f"Credential field is not registered: {field_name}",
            content={"error_code": CREDENTIAL_FIELD_MISSING, "credential_key": key, "field_ref": field_name},
        )
    if not field.is_secret():
        return ResolvedCredentialField(
            field_name=field_name,
            value=field.value or "",
            source="catalog_metadata",
            source_ref={},
            secret=False,
        )
    source_failed = False
    for source_index, source_ref in enumerate(field.resolution_order):
        try:
            resolved = _resolve_source_ref(
                source_ref,
                env=env,
                scope=resolved_scope,
                expires_at=credential.expires_at,
                now=now,
            )
        except Exception:
            # Secret adapters may include credential material in exceptions.
            # Preserve configured failover without reflecting their messages.
            source_failed = True
            _LOGGER.warning(
                "event=credential_source_resolution_failed source_index=%s failure_kind=exception",
                source_index,
            )
            continue
        if isinstance(resolved, ToolResult):
            if str((resolved.content or {}).get("error_code") or "") == CREDENTIAL_EXPIRED:
                return resolved
            source_failed = True
            continue
        if resolved:
            return ResolvedCredentialField(
                field_name=field_name,
                value=resolved,
                source=source_ref.source,
                source_ref=_redacted_source_ref(source_ref),
                secret=True,
            )
    code = (
        CREDENTIAL_SOURCES_UNCONFIGURED if not field.resolution_order
        else CREDENTIAL_SOURCE_FAILED if source_failed
        else CREDENTIAL_VALUE_UNAVAILABLE
    )
    return ToolResult(
        status="error",
        error=f"Credential field could not be resolved: {key}.{field_name}",
        content={"error_code": code, "credential_key": key, "field_ref": field_name},
    )


def _resolve_source_ref(
    source_ref: CatalogSourceRef,
    *,
    env: Mapping[str, str] | None = None,
    scope: CredentialOwnerScope,
    expires_at: str | None = None,
    now: datetime | None = None,
) -> str | ToolResult | None:
    if source_ref.source == "env":
        if not source_ref.env_var:
            return None
        return _resolve_env_value_with_team_priority(
            source_ref.env_var,
            env=env,
            file_fallback=source_ref.file_fallback,
        )
    source = get_credential_source(source_ref.source)
    if source is None:
        return ToolResult(status="error", error=f"Credential source is not registered: {source_ref.source}")
    cache_key = _secret_cache_key(source_ref, scope)
    current_utc = now if now is not None else datetime.now(timezone.utc)
    caller_hard_expiry: datetime | None = None
    remaining_seconds: float | None = None
    if expires_at is not None:
        caller_hard_expiry = parse_utc_timestamp(expires_at)
        remaining_seconds = (caller_hard_expiry - current_utc).total_seconds()
        if remaining_seconds <= 0:
            _SECRET_CACHE.pop(cache_key, None)
            return ToolResult(
                status="error",
                error="Temporary credential has expired.",
                content={"error_code": CREDENTIAL_EXPIRED, "expires_at": expires_at},
            )
    ttl_seconds = _default_secret_cache_ttl_seconds()
    effective_ttl = (
        min(float(ttl_seconds), remaining_seconds)
        if remaining_seconds is not None
        else float(ttl_seconds)
    )
    if effective_ttl > 0:
        mono_now = time.monotonic()
        cached = _SECRET_CACHE.get(cache_key)
        if cached is not None:
            cached_value, cached_mono_deadline, cached_hard_expiry = cached
            effective_hard_expiry = (
                min(caller_hard_expiry, cached_hard_expiry)
                if caller_hard_expiry is not None and cached_hard_expiry is not None
                else (caller_hard_expiry or cached_hard_expiry)
            )
            if effective_hard_expiry is not None:
                hard_remaining = (effective_hard_expiry - current_utc).total_seconds()
                if hard_remaining <= 0:
                    _SECRET_CACHE.pop(cache_key, None)
                    if caller_hard_expiry is not None:
                        return ToolResult(
                            status="error",
                            error="Temporary credential has expired.",
                            content={"error_code": CREDENTIAL_EXPIRED, "expires_at": expires_at},
                        )
                elif cached_mono_deadline > mono_now:
                    refresh_ttl = min(float(ttl_seconds), hard_remaining)
                    _SECRET_CACHE[cache_key] = (
                        cached_value,
                        mono_now + refresh_ttl,
                        effective_hard_expiry,
                    )
                    return cached_value
                else:
                    _SECRET_CACHE.pop(cache_key, None)
            elif cached_mono_deadline > mono_now:
                _SECRET_CACHE[cache_key] = (cached_value, mono_now + float(ttl_seconds), None)
                return cached_value
            else:
                _SECRET_CACHE.pop(cache_key, None)
    owner_scope_kind = scope.scope_kind
    owner_id = scope.owner_id
    request = CredentialSecretRequest(
        ref=CredentialRef(
            source_provider=source_ref.source,
            item_id=source_ref.item_id,
            item_name=source_ref.item_name,
            vault=source_ref.vault,
            folder=source_ref.folder,
            owner_scope_kind=owner_scope_kind,
            owner_id=owner_id,
        ),
        field_ref=source_ref.field_ref,
        owner_scope_kind=owner_scope_kind,
        owner_id=owner_id,
    )
    if owner_id and active_credential_owner_id() != owner_id:
        with credential_owner_context(owner_id):
            result = source.get_secret(request)
    else:
        result = source.get_secret(request)
    if isinstance(result, ToolResult):
        return result
    resolved = _string(result) or None
    if caller_hard_expiry is not None:
        post_now = now if now is not None else datetime.now(timezone.utc)
        post_remaining = (caller_hard_expiry - post_now).total_seconds()
        if post_remaining <= 0:
            _SECRET_CACHE.pop(cache_key, None)
            return ToolResult(
                status="error",
                error="Temporary credential has expired.",
                content={"error_code": CREDENTIAL_EXPIRED, "expires_at": expires_at},
            )
    else:
        post_remaining = None
    if resolved and ttl_seconds > 0:
        store_ttl = (
            min(float(ttl_seconds), post_remaining)
            if post_remaining is not None
            else float(ttl_seconds)
        )
        if store_ttl > 0:
            _SECRET_CACHE[cache_key] = (resolved, time.monotonic() + store_ttl, caller_hard_expiry)
    return resolved


def _sealbox_record_key(source_ref: CatalogSourceRef) -> str | None:
    """Canonical Sealbox record key addressed by ``source_ref``, or None for non-Sealbox refs.

    Matches ``SealboxCredentialSource.get_secret`` so two catalog entries naming
    the same Sealbox record (including shorthand ``field_ref`` forms) map to the
    exact same key.
    """

    if normalize_credential_source_name(source_ref.source) != "sealbox":
        return None
    key = _string(source_ref.item_id or source_ref.item_name)
    if not key:
        return None
    field_ref = _string(source_ref.field_ref or "password")
    if field_ref in {"", "password", "value"}:
        return key
    return f"{key}:{field_ref}"


def _credential_sealbox_record_keys(credential: CatalogCredential) -> set[str]:
    keys: set[str] = set()
    for field in credential.fields.values():
        for source_ref in field.resolution_order:
            record_key = _sealbox_record_key(source_ref)
            if record_key:
                keys.add(record_key)
    return keys


def _planned_sealbox_record_keys(
    *,
    credential_key: str,
    request_payload: Mapping[str, Any],
    values: Mapping[str, str],
    field_specs: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    item_key = _sealbox_catalog_key(credential_key, request_payload)
    keys: set[str] = set()
    username = _first_value_by_kind(values, field_specs, {"username", "email", "login"})
    password_field = _secret_password_field_name(field_specs)
    if password_field and username:
        keys.add(item_key)
    elif password_field:
        keys.add(
            f"{item_key}:{safe_key_component(_field_storage_ref(password_field, field_specs.get(password_field, {})))}"
        )
    for field_name, spec in field_specs.items():
        if field_name == password_field or not _field_is_secret(spec):
            continue
        field_ref = safe_key_component(_field_storage_ref(field_name, spec), fallback=field_name)
        keys.add(f"{item_key}:{field_ref}")
    return keys


def _find_shared_sealbox_record(
    catalog: CredentialCatalog,
    *,
    exclude_key: str,
    target_keys: set[str],
    temporary_only: bool,
) -> tuple[str, str] | None:
    if not target_keys:
        return None
    for other_key, other_credential in catalog.credentials.items():
        if other_key == exclude_key:
            continue
        if temporary_only and other_credential.expires_at is None:
            continue
        overlap = _credential_sealbox_record_keys(other_credential) & target_keys
        if overlap:
            return other_key, sorted(overlap)[0]
    return None


def _sealbox_delete_targets_for_credential(
    credential: CatalogCredential,
    *,
    keep_keys: set[str] | None = None,
) -> list[tuple[str, str]]:
    keep = keep_keys or set()
    target_kind_by_item: dict[str, str] = {}
    ordered_items: list[str] = []
    for field in credential.fields.values():
        for source_ref in field.resolution_order:
            record_key = _sealbox_record_key(source_ref)
            if not record_key or record_key in keep:
                continue
            item_kind = _sealbox_ref_kind(source_ref)
            if record_key not in target_kind_by_item:
                ordered_items.append(record_key)
                target_kind_by_item[record_key] = item_kind
            elif item_kind == "credential":
                target_kind_by_item[record_key] = "credential"
    return [(item_key, target_kind_by_item[item_key]) for item_key in ordered_items]


def _delete_sealbox_targets(
    targets: list[tuple[str, str]],
    *,
    scope: CredentialOwnerScope,
    credential_key: str,
    event_name: str,
) -> list[str]:
    if not targets:
        return []
    tenant_env = sealbox_tenant_environment(
        scope_kind=scope.scope_kind,
        owner_id=scope.owner_id,
        provision=False,
    )
    if isinstance(tenant_env, ToolResult):
        if (tenant_env.content or {}).get("error_code") == SEALBOX_TENANT_NOT_PROVISIONED:
            return []
        _LOGGER.warning(
            "Failed to open Sealbox tenant environment while cleaning up records. "
            "event=%s scope_kind=%s owner_id=%s credential_key=%s error=%s",
            event_name,
            scope.scope_kind,
            scope.owner_id,
            credential_key,
            tenant_env.error,
        )
        return [item_key for item_key, _kind in targets]
    failed: list[str] = []
    for item_key, item_kind in targets:
        deleted = delete_sealbox_item(item_key=item_key, item_kind=item_kind, env=tenant_env)
        if isinstance(deleted, ToolResult):
            failed.append(item_key)
            _LOGGER.warning(
                "Failed to delete Sealbox record during cleanup. "
                "event=%s scope_kind=%s owner_id=%s credential_key=%s item_key=%s error=%s",
                event_name,
                scope.scope_kind,
                scope.owner_id,
                credential_key,
                item_key,
                deleted.error,
            )
    return failed


def _pending_dropped_sealbox_conflict(scope: CredentialOwnerScope, target_keys: set[str]) -> str | None:
    """Return the first record key in ``target_keys`` still queued for dropped-record deletion."""

    if not target_keys:
        return None
    db_path = resolve_catalog_db_path()
    if not db_path.is_file():
        return None
    overlap = CredentialCatalogStore(db_path).pending_dropped_sealbox_keys(scope) & target_keys
    return sorted(overlap)[0] if overlap else None


def _resolve_pending_dropped_sealbox_conflict(
    scope: CredentialOwnerScope,
    target_keys: set[str],
    *,
    catalog: CredentialCatalog,
    credential_key: str,
) -> str | None:
    """Retry any queued dropped-record deletions overlapping ``target_keys`` before writing to Sealbox.

    Must only be called while holding ``_scope_catalog_lock(scope)`` and strictly
    before writing new secret values to Sealbox. Clears stale queue rows whose
    records are referenced again in ``catalog``; attempts immediate deletion for
    remaining overlapping rows so a recovered Sealbox never locks out saves for
    up to 6 hours; returns the first record key whose deletion still failed.
    """

    if not target_keys:
        return None
    db_path = resolve_catalog_db_path()
    if not db_path.is_file():
        return None
    store = CredentialCatalogStore(db_path)
    overlapping = [
        entry
        for entry in store.list_pending_dropped_sealbox_deletes(scope=scope)
        if entry.item_key in target_keys
    ]
    if not overlapping:
        return None
    referenced_keys: set[str] = set()
    for cred in catalog.credentials.values():
        referenced_keys |= _credential_sealbox_record_keys(cred)
    stale_keys: list[str] = []
    targets_to_delete: list[tuple[str, str]] = []
    for entry in overlapping:
        if entry.item_key in referenced_keys:
            stale_keys.append(entry.item_key)
        else:
            targets_to_delete.append((entry.item_key, entry.item_kind))
    if stale_keys:
        store.remove_pending_dropped_sealbox_deletes(scope, stale_keys)
    if not targets_to_delete:
        return None
    still_failed = _delete_and_clear_dropped_targets(
        targets_to_delete,
        scope=scope,
        credential_key=credential_key,
        event_name="credential_dropped_sealbox_inline_retry_failed",
    )
    return sorted(still_failed)[0] if still_failed else None


def _pending_dropped_cleanup_conflict_result(credential_key: str, record_key: str) -> ToolResult:
    return ToolResult(
        status="error",
        error=(
            f"Cannot use Sealbox record {record_key} for {credential_key} because an earlier value "
            "is still waiting to be deleted. The broker retries deletion automatically within 6 hours."
        ),
        content={
            "error_code": CREDENTIAL_DROPPED_CLEANUP_PENDING,
            "credential_key": credential_key,
            "sealbox_record_key": record_key,
        },
    )


def _dropped_sealbox_records_form_only_result(
    credential_key: str,
    dropped_keys: set[str],
) -> ToolResult:
    ordered = sorted(dropped_keys)
    return ToolResult(
        status="error",
        error=(
            f"Cannot replace {credential_key} because the save would drop stored Sealbox record(s) "
            f"({', '.join(ordered)}). Deleting or replacing a person's stored credential requires the "
            "person's own action. Ask the person to update or delete the credential through the "
            "secure broker form (secure_credential_collection_request)."
        ),
        content={
            "error_code": CREDENTIAL_REPLACEMENT_FORM_ONLY,
            "credential_key": credential_key,
            "dropped_sealbox_records": ordered,
            "form_creation_action": "secure_credential_collection_request",
            "next_step": (
                "Ask the person to submit a secure credential form "
                "(action_type=secure_credential_collection_request) to replace or delete this credential."
            ),
        },
    )


def _delete_and_clear_dropped_targets(
    dropped_targets: list[tuple[str, str]],
    *,
    scope: CredentialOwnerScope,
    credential_key: str,
    event_name: str,
) -> list[str]:
    if not dropped_targets:
        return []
    try:
        failed = _delete_sealbox_targets(
            dropped_targets,
            scope=scope,
            credential_key=credential_key,
            event_name=event_name,
        )
    except Exception as exc:  # noqa: BLE001 - catalog row is already committed; keep targets queued for retry
        _LOGGER.warning(
            "Unexpected error while deleting dropped Sealbox records; keeping retry entries. "
            "event=%s scope_kind=%s owner_id=%s credential_key=%s error=%s",
            event_name,
            scope.scope_kind,
            scope.owner_id,
            credential_key,
            type(exc).__name__,
        )
        return [item_key for item_key, _kind in dropped_targets]
    failed_set = set(failed)
    succeeded = [item_key for item_key, _kind in dropped_targets if item_key not in failed_set]
    if succeeded:
        try:
            CredentialCatalogStore(resolve_catalog_db_path()).remove_pending_dropped_sealbox_deletes(
                scope, succeeded
            )
        except Exception as exc:  # noqa: BLE001 - next janitor retry treats already_absent as deleted
            _LOGGER.warning(
                "Deleted dropped Sealbox records but could not clear retry queue rows. "
                "event=credential_dropped_sealbox_queue_clear_failed scope_kind=%s owner_id=%s "
                "credential_key=%s error=%s",
                scope.scope_kind,
                scope.owner_id,
                credential_key,
                type(exc).__name__,
            )
    return failed


def _save_credential_and_cleanup_dropped(
    credential_key: str,
    credential: CatalogCredential,
    *,
    existing_credential: CatalogCredential | None,
    scope: CredentialOwnerScope,
    queued_by: str = "form",
) -> list[str]:
    """Commit ``credential`` and delete dropped Sealbox records while holding ``_scope_catalog_lock``.

    Queues dropped Sealbox records in ``pending_dropped_sealbox_deletes`` inside
    the same SQLite transaction as the catalog write, then deletes every version
    while ``_scope_catalog_lock`` still blocks other writers in the same scope
    from adopting them. Returns any record keys whose deletion failed (and
    therefore remain queued for the broker janitor or transfer retry).
    """

    dropped_targets = (
        _sealbox_delete_targets_for_credential(
            existing_credential,
            keep_keys=_credential_sealbox_record_keys(credential),
        )
        if existing_credential is not None
        else []
    )
    _save_catalog_credential(
        credential_key,
        credential,
        scope=scope,
        dropped_sealbox_targets=dropped_targets,
        queued_by=queued_by,
    )
    _SECRET_CACHE.clear()
    return _delete_and_clear_dropped_targets(
        dropped_targets,
        scope=scope,
        credential_key=credential_key,
        event_name="credential_dropped_sealbox_delete_failed",
    )


@dataclass(frozen=True)
class DroppedSealboxCleanupReport:
    """Counts from retrying queued dropped-Sealbox-record deletions."""

    scanned: int = 0
    deleted: int = 0
    skipped_referenced: int = 0
    failed: int = 0
    failed_keys: tuple[str, ...] = ()


def retry_pending_dropped_sealbox_deletes(
    *,
    scope: CredentialOwnerScope | None = None,
    credential_key: str | None = None,
) -> DroppedSealboxCleanupReport:
    """Retry queued dropped-Sealbox-record deletions, isolating errors per owner scope."""

    db_path = resolve_catalog_db_path()
    if not db_path.is_file():
        return DroppedSealboxCleanupReport()
    store = CredentialCatalogStore(db_path)
    initial = store.list_pending_dropped_sealbox_deletes(scope=scope, credential_key=credential_key)
    if not initial:
        return DroppedSealboxCleanupReport()
    scopes: list[CredentialOwnerScope] = []
    seen_scopes: set[CredentialOwnerScope] = set()
    for entry in initial:
        if entry.scope not in seen_scopes:
            seen_scopes.add(entry.scope)
            scopes.append(entry.scope)
    scanned = 0
    deleted = 0
    skipped_referenced = 0
    failed = 0
    failed_keys: list[str] = []
    for row_scope in scopes:
        scope_initial = [entry for entry in initial if entry.scope == row_scope]
        pending: list[PendingDroppedSealboxDelete] = scope_initial
        counted_scope = False
        try:
            with _scope_catalog_lock(row_scope):
                pending = store.list_pending_dropped_sealbox_deletes(
                    scope=row_scope,
                    credential_key=credential_key,
                )
                if not pending:
                    continue
                scanned += len(pending)
                counted_scope = True
                catalog = load_catalog(scope=row_scope)
                referenced_keys: set[str] = set()
                for cred in catalog.credentials.values():
                    referenced_keys |= _credential_sealbox_record_keys(cred)
                stale_keys: list[str] = []
                targets_to_delete: list[tuple[str, str]] = []
                for entry in pending:
                    if entry.item_key in referenced_keys:
                        stale_keys.append(entry.item_key)
                        skipped_referenced += 1
                        _LOGGER.warning(
                            "Skipped pending dropped Sealbox deletion because a catalog entry references the record again. "
                            "event=credential_pending_dropped_delete_skipped_referenced scope_kind=%s owner_id=%s "
                            "item_key=%s credential_key=%s",
                            row_scope.scope_kind,
                            row_scope.owner_id,
                            entry.item_key,
                            entry.credential_key,
                        )
                    else:
                        targets_to_delete.append((entry.item_key, entry.item_kind))
                if stale_keys:
                    store.remove_pending_dropped_sealbox_deletes(row_scope, stale_keys)
                if targets_to_delete:
                    scope_failed = _delete_and_clear_dropped_targets(
                        targets_to_delete,
                        scope=row_scope,
                        credential_key=credential_key or pending[0].credential_key,
                        event_name="credential_dropped_sealbox_retry_failed",
                    )
                    deleted += len(targets_to_delete) - len(scope_failed)
                    failed += len(scope_failed)
                    failed_keys.extend(scope_failed)
        except Exception as exc:  # noqa: BLE001 - isolate one owner's unreadable catalog from other owners
            if not counted_scope:
                scanned += len(pending)
            failed += len(pending)
            failed_keys.extend(entry.item_key for entry in pending)
            _LOGGER.warning(
                "Failed to retry pending dropped Sealbox deletions for one owner; continuing with other owners. "
                "event=credential_dropped_sealbox_retry_scope_failed scope_kind=%s owner_id=%s "
                "pending_count=%s error=%s",
                row_scope.scope_kind,
                row_scope.owner_id,
                len(pending),
                type(exc).__name__,
            )
    return DroppedSealboxCleanupReport(
        scanned=scanned,
        deleted=deleted,
        skipped_referenced=skipped_referenced,
        failed=failed,
        failed_keys=tuple(failed_keys),
    )


def _rollback_new_sealbox_writes(
    written_credential: CatalogCredential,
    *,
    existing_keys: set[str],
    catalog: CredentialCatalog,
    credential_key: str,
    scope: CredentialOwnerScope,
) -> bool:
    """Delete brand-new Sealbox records written by a failed save; return True if any written record remains."""

    written_targets = _sealbox_delete_targets_for_credential(written_credential)
    if not written_targets:
        return False
    other_keys: set[str] = set()
    for other_key, other_cred in catalog.credentials.items():
        if other_key != credential_key:
            other_keys |= _credential_sealbox_record_keys(other_cred)
    protected = existing_keys | other_keys
    rollback_targets = [(key, kind) for key, kind in written_targets if key not in protected]
    unrolled_protected = len(rollback_targets) < len(written_targets)
    failed = _delete_sealbox_targets(
        rollback_targets,
        scope=scope,
        credential_key=credential_key,
        event_name="credential_failed_write_rollback_failed",
    )
    return unrolled_protected or bool(failed)


def sealbox_record_expiry(
    scope: CredentialOwnerScope,
    *,
    item_id: str | None = None,
    item_name: str | None = None,
    field_ref: str | None = None,
) -> str | None:
    """Return the ``expires_at`` timestamp if a temporary catalog entry in ``scope`` owns this Sealbox record."""

    target_key = _sealbox_record_key(
        CatalogSourceRef(
            source="sealbox",
            item_id=item_id,
            item_name=item_name,
            field_ref=field_ref or "password",
        )
    )
    if not target_key:
        return None
    rows = load_scope_readonly(resolve_catalog_db_path(), scope)
    for raw in rows.values():
        if not isinstance(raw, Mapping):
            continue
        raw_expires = raw.get("expires_at")
        if raw_expires is None or (isinstance(raw_expires, str) and not raw_expires.strip()):
            continue
        credential = CatalogCredential.model_validate(raw)
        if target_key in _credential_sealbox_record_keys(credential):
            return credential.expires_at
    return None


def _secret_cache_key(
    source_ref: CatalogSourceRef,
    scope: CredentialOwnerScope,
) -> tuple[str, str, str, str | None, str | None, str | None, str | None, str]:
    return (
        scope.scope_kind,
        scope.owner_id,
        source_ref.source,
        source_ref.item_id,
        source_ref.item_name,
        source_ref.vault,
        source_ref.folder,
        source_ref.field_ref,
    )


def generate_password(
    *,
    write_source: str | None = None,
    length: int = DEFAULT_PASSWORD_LENGTH,
    field_spec: Mapping[str, Any] | None = None,
) -> str | ToolResult:
    source = normalize_credential_source_name(write_source or "")
    policy_args = _sealbox_password_policy_args(field_spec or {}, length=length)
    if source == "sealbox":
        return _generate_password_with_sealbox(policy_args=policy_args)
    # Bitwarden remains the native generator for Bitwarden writes and the legacy fallback.
    return _generate_password_with_bitwarden(length=length)


def _sealbox_password_policy_args(field_spec: Mapping[str, Any], *, length: int = DEFAULT_PASSWORD_LENGTH) -> list[str]:
    policy = field_spec.get("password_policy") if isinstance(field_spec.get("password_policy"), Mapping) else {}
    generation = field_spec.get("generation") if isinstance(field_spec.get("generation"), Mapping) else {}
    options = {**dict(policy or {}), **dict(generation or {})}
    raw_length = (
        options.get("length")
        or field_spec.get("password_length")
        or field_spec.get("generation_length")
        or field_spec.get("length")
        or length
    )
    try:
        parsed_length = max(4, int(raw_length))
    except (TypeError, ValueError):
        parsed_length = max(4, int(length))
    args = ["--length", str(parsed_length)]
    generation_policy = _string(field_spec.get("generation_policy")).lower()
    if bool(options.get("alphanumeric")) or "alphanumeric" in generation_policy:
        args.append("--alphanumeric")
    if bool(options.get("exclude_ambiguous")) or "exclude_ambiguous" in generation_policy:
        args.append("--exclude-ambiguous")
    for option_name, cli_flag in (
        ("no_symbols", "--no-symbols"),
        ("no_numbers", "--no-numbers"),
        ("no_uppercase", "--no-uppercase"),
        ("no_lowercase", "--no-lowercase"),
    ):
        if bool(options.get(option_name)):
            args.append(cli_flag)
    return args


def _generate_password_with_sealbox(*, policy_args: list[str] | None = None) -> str | ToolResult:
    command = ["password", "generate", "--output", "json", *(policy_args or ["--length", str(DEFAULT_PASSWORD_LENGTH)])]
    completed = run_sealbox(command, timeout_seconds=15)
    if isinstance(completed, ToolResult):
        return completed
    content = sealbox_redacted_completed_content(
        completed,
        action="password.generate",
        command=["sealbox-cli", *command],
    )
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"sealbox-cli password generate exited with code {completed.returncode}")
    parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, Mapping))
    if not isinstance(parsed, Mapping):
        return ToolResult(status="error", content=content, error="sealbox-cli password generate returned non-JSON output.")
    password = _string(parsed.get("password"))
    if not password:
        return ToolResult(status="error", error="sealbox-cli password generate returned an empty password.")
    return password


def _generate_password_with_bitwarden(*, length: int = DEFAULT_PASSWORD_LENGTH) -> str | ToolResult:
    executable = shutil.which("bw")
    if not executable:
        return ToolResult(
            status="error",
            error="Password generation unavailable: Bitwarden CLI is not installed or not on PATH.",
            content={"error_code": "password_generation_unavailable", "fallback_source": "bitwarden"},
        )
    config_result = ensure_bitwarden_server_configured_from_env()
    if config_result is not None:
        return config_result
    command = [
        executable,
        "generate",
        "--length",
        str(max(16, int(length))),
        "--uppercase",
        "--lowercase",
        "--number",
        "--special",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error="bw generate timed out after 15s")
    except Exception as exc:
        return ToolResult(status="error", error=f"bw generate failed to start: {exc}")
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            error=f"bw generate exited with code {completed.returncode}",
            content={"stderr": redact_text(completed.stderr or "").strip(), "error_code": "password_generation_unavailable"},
        )
    password = _string(completed.stdout)
    if not password:
        return ToolResult(status="error", error="bw generate returned an empty password.")
    return password


def resolve_credential_write_source(request_payload: Mapping[str, Any]) -> str:
    """Resolve the effective write source name for a credential collection request."""

    write_source = normalize_credential_source_name(
        _string(request_payload.get("write_source"))
        or _string(request_payload.get("store_target"))
        or _string(os.getenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE"))
        or "sealbox"
    )
    if write_source == "default":
        write_source = normalize_credential_source_name(
            _string(os.getenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE")) or "sealbox"
        )
    return write_source


def collect_credential(
    request_payload: Mapping[str, Any],
    submitted_fields: Mapping[str, str],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
    expires_at: datetime | str | None = None,
    allow_expiry_change: bool = False,
    now: datetime | None = None,
) -> ToolResult:
    """Store a credential from trusted form submission or broker-side storage.

    ``expires_at`` is accepted only as an explicit keyword argument (never read
    from ``request_payload``) and only when ``allow_expiry_change=True`` (set
    by the secure form submission handler, team transfer import, and trusted
    broker capture). When ``allow_expiry_change=False`` (the default, used by
    agent-callable ``credential.collect``), overwriting a key that currently
    has ``expires_at`` preserves that expiry and writes every Sealbox record
    with the remaining TTL (or refuses if the expiry has already passed), and
    any save that would drop Sealbox records referenced by the existing key is
    refused with ``credential_replacement_form_only`` before writing anything.
    """

    try:
        resolved_scope = _owner_scope(context=context, scope=scope)
        credential_key = _credential_key(request_payload.get("credential_key"))
        write_source = resolve_credential_write_source(request_payload)
        current_now = now if now is not None else datetime.now(timezone.utc)
        normalized_expires_at: str | None = None
        ttl_seconds: int | None = None
        if expires_at is not None:
            if not allow_expiry_change:
                return ToolResult(
                    status="error",
                    error=(
                        "Temporary credential expiry can only be changed through the secure "
                        "broker form or trusted transfer import."
                    ),
                    content={"error_code": CREDENTIAL_EXPIRY_FORM_ONLY, "credential_key": credential_key},
                )
            try:
                validated_expiry = validate_storage_expiry(expires_at, now=current_now)
            except CredentialExpiryError as exc:
                return ToolResult(
                    status="error",
                    error=exc.message,
                    content={"error_code": exc.code, "credential_key": credential_key},
                )
            if write_source != "sealbox":
                return ToolResult(
                    status="error",
                    error=(
                        "Temporary credentials with an expiry are only supported for Sealbox storage, "
                        f"not {write_source or '<empty>'}."
                    ),
                    content={
                        "error_code": CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE,
                        "write_source": write_source,
                        "credential_key": credential_key,
                    },
                )
            normalized_expires_at = format_utc_timestamp(validated_expiry)
            ttl_seconds = remaining_ttl_seconds(validated_expiry, now=current_now)
        field_specs = _normalize_field_specs(request_payload.get("fields") or [])
        if not field_specs:
            return ToolResult(
                status="error",
                error=(
                    "credential.collect requires at least one credential field spec. "
                    "The broker must normalize the secure collection request into explicit fields before storage."
                ),
                content={
                    "error_code": "credential_fields_required",
                    "form_creation_action": "secure_credential_collection_request",
                },
            )
        values: dict[str, str] = {}
        secret_fields: dict[str, str] = {}
        public_fields: dict[str, str] = {}
        generated_secret_fields: set[str] = set()
        active_field_specs: dict[str, dict[str, Any]] = {}
        pending_non_sealbox_generated: list[tuple[str, dict[str, Any]]] = []
        # Validate every field before calling any password generator or writing
        # anything to the credential store so refusals leave no partial records.
        for field_name, field_spec in field_specs.items():
            source = _string(field_spec.get("value_source")) or "user_input"
            secret = is_secret_credential_field(field_spec)
            if secret and _string(field_spec.get("value")):
                return ToolResult(
                    status="error",
                    error=(
                        f"Secret field {field_name} included a value in the request payload. "
                        "Submit secret values through the broker form or store-native generation only."
                    ),
                    content={"error_code": "secret_value_in_request_rejected", "field_ref": field_name},
                )
            required = bool(field_spec.get("required", True))
            if source == "store_generated_secret":
                active_field_specs[field_name] = field_spec
                if write_source == "sealbox":
                    generated_secret_fields.add(field_name)
                    values[field_name] = ""
                    if secret:
                        secret_fields[field_name] = ""
                else:
                    pending_non_sealbox_generated.append((field_name, field_spec))
                continue
            if source in {"agent_supplied_metadata", "user_or_agent_supplied_metadata"}:
                value = _string(field_spec.get("value")) or _string(submitted_fields.get(field_name))
            else:
                value = _string(submitted_fields.get(field_name))
            if not value and required:
                return ToolResult(
                    status="error",
                    error=(
                        f"Missing required credential field: {field_name}. "
                        "credential.collect stores credentials after values have been received through a secure "
                        "broker form or generated by the credential store; it does not create a user-facing form. "
                        "To collect values from the user, call the StructuredAgentTurnDecision "
                        "structured-output tool with action_type=secure_credential_collection_request through the gateway."
                    ),
                    content={
                        "error_code": "missing_required_credential_field",
                        "field_ref": field_name,
                        "form_creation_action": "secure_credential_collection_request",
                    },
                )
            # Optional secret fields left blank are omitted from both the secret
            # store and the catalog entry so callers observe credential_field_missing.
            if not value and not required and secret:
                continue
            active_field_specs[field_name] = field_spec
            values[field_name] = value
            if secret:
                secret_fields[field_name] = value
            else:
                public_fields[field_name] = value
        with credential_key_lock(resolved_scope, credential_key):
            existing_keys: set[str] = set()
            with _scope_catalog_lock(resolved_scope):
                catalog_snapshot = load_catalog(scope=resolved_scope)
                existing_credential = catalog_snapshot.credentials.get(credential_key)
                if not allow_expiry_change and existing_credential is not None and existing_credential.expires_at is not None:
                    lock_now = now if now is not None else datetime.now(timezone.utc)
                    if is_expired(existing_credential.expires_at, now=lock_now):
                        return ToolResult(
                            status="error",
                            error=(
                                f"Temporary credential {credential_key} has already expired and cannot be "
                                "overwritten without the person submitting the secure form."
                            ),
                            content={
                                "error_code": CREDENTIAL_EXPIRED,
                                "credential_key": credential_key,
                                "expires_at": existing_credential.expires_at,
                            },
                        )
                    if write_source != "sealbox":
                        return ToolResult(
                            status="error",
                            error=(
                                "Temporary credentials with an expiry are only supported for Sealbox storage, "
                                f"not {write_source or '<empty>'}."
                            ),
                            content={
                                "error_code": CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE,
                                "write_source": write_source,
                                "credential_key": credential_key,
                            },
                        )
                    normalized_expires_at = existing_credential.expires_at
                    ttl_seconds = remaining_ttl_seconds(existing_credential.expires_at, now=lock_now)
                existing_keys = (
                    _credential_sealbox_record_keys(existing_credential)
                    if existing_credential is not None
                    else set()
                )
                planned_keys = (
                    _planned_sealbox_record_keys(
                        credential_key=credential_key,
                        request_payload=request_payload,
                        values=values,
                        field_specs=active_field_specs,
                    )
                    if write_source == "sealbox"
                    else set()
                )
                dropped_keys = existing_keys - planned_keys
                if dropped_keys:
                    if not allow_expiry_change:
                        return _dropped_sealbox_records_form_only_result(credential_key, dropped_keys)
                    shared_dropped = _find_shared_sealbox_record(
                        catalog_snapshot,
                        exclude_key=credential_key,
                        target_keys=dropped_keys,
                        temporary_only=False,
                    )
                    if shared_dropped is not None:
                        other_key, record_key = shared_dropped
                        return ToolResult(
                            status="error",
                            error=(
                                f"Cannot replace {credential_key} because another catalog entry "
                                f"({other_key}) still references Sealbox record {record_key}."
                            ),
                            content={
                                "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                                "credential_key": credential_key,
                                "conflicting_credential_key": other_key,
                                "sealbox_record_key": record_key,
                            },
                        )
                if write_source == "sealbox":
                    pending_conflict = _resolve_pending_dropped_sealbox_conflict(
                        resolved_scope,
                        planned_keys,
                        catalog=catalog_snapshot,
                        credential_key=credential_key,
                    )
                    if pending_conflict is not None:
                        return _pending_dropped_cleanup_conflict_result(credential_key, pending_conflict)
                    shared = _find_shared_sealbox_record(
                        catalog_snapshot,
                        exclude_key=credential_key,
                        target_keys=planned_keys,
                        temporary_only=normalized_expires_at is None,
                    )
                    if shared is not None:
                        other_key, record_key = shared
                        error_message = (
                            f"Cannot store an expiry on {credential_key} because another catalog entry "
                            f"({other_key}) also references the same Sealbox record."
                            if normalized_expires_at is not None
                            else f"Cannot store {credential_key} onto a Sealbox record owned by temporary "
                            f"credential {other_key}."
                        )
                        return ToolResult(
                            status="error",
                            error=error_message,
                            content={
                                "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                                "credential_key": credential_key,
                                "conflicting_credential_key": other_key,
                                "sealbox_record_key": record_key,
                            },
                        )
            for field_name, field_spec in pending_non_sealbox_generated:
                generated = generate_password(write_source=write_source, field_spec=field_spec)
                if isinstance(generated, ToolResult):
                    return generated
                values[field_name] = generated
                secret_fields[field_name] = generated
            write_result = write_credential_to_source(
                write_source=write_source,
                credential_key=credential_key,
                request_payload=request_payload,
                values=values,
                field_specs=active_field_specs,
                generated_secret_fields=generated_secret_fields,
                scope=resolved_scope,
                ttl_seconds=ttl_seconds,
            )
            if isinstance(write_result, ToolResult):
                return write_result
            credential = build_catalog_credential(
                credential_key=credential_key,
                request_payload=request_payload,
                field_specs=active_field_specs,
                public_fields=public_fields,
                write_result=write_result,
                expires_at=normalized_expires_at,
            )
            latest_existing: CatalogCredential | None = None
            post_write_conflict: ToolResult | None = None
            latest_catalog_for_rollback: CredentialCatalog | None = None
            save_error: Exception | None = None
            failed_dropped: list[str] = []
            with _scope_catalog_lock(resolved_scope):
                latest_catalog = load_catalog(scope=resolved_scope)
                latest_catalog_for_rollback = latest_catalog
                latest_existing = latest_catalog.credentials.get(credential_key)
                actual_new_keys = _credential_sealbox_record_keys(credential)
                actual_existing_keys = (
                    _credential_sealbox_record_keys(latest_existing)
                    if latest_existing is not None
                    else set()
                )
                actual_dropped_keys = actual_existing_keys - actual_new_keys
                if actual_dropped_keys:
                    if not allow_expiry_change:
                        post_write_conflict = _dropped_sealbox_records_form_only_result(
                            credential_key, actual_dropped_keys
                        )
                    else:
                        shared_dropped = _find_shared_sealbox_record(
                            latest_catalog,
                            exclude_key=credential_key,
                            target_keys=actual_dropped_keys,
                            temporary_only=False,
                        )
                        if shared_dropped is not None:
                            other_key, record_key = shared_dropped
                            post_write_conflict = ToolResult(
                                status="error",
                                error=(
                                    f"Cannot replace {credential_key} because another catalog entry "
                                    f"({other_key}) still references Sealbox record {record_key}."
                                ),
                                content={
                                    "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                                    "credential_key": credential_key,
                                    "conflicting_credential_key": other_key,
                                    "sealbox_record_key": record_key,
                                },
                            )
                if post_write_conflict is None and write_source == "sealbox":
                    pending_conflict = _pending_dropped_sealbox_conflict(resolved_scope, actual_new_keys)
                    if pending_conflict is not None:
                        post_write_conflict = _pending_dropped_cleanup_conflict_result(
                            credential_key, pending_conflict
                        )
                if post_write_conflict is None and write_source == "sealbox":
                    shared = _find_shared_sealbox_record(
                        latest_catalog,
                        exclude_key=credential_key,
                        target_keys=actual_new_keys,
                        temporary_only=normalized_expires_at is None,
                    )
                    if shared is not None:
                        other_key, record_key = shared
                        error_message = (
                            f"Cannot store an expiry on {credential_key} because another catalog entry "
                            f"({other_key}) also references the same Sealbox record."
                            if normalized_expires_at is not None
                            else f"Cannot store {credential_key} onto a Sealbox record owned by temporary "
                            f"credential {other_key}."
                        )
                        post_write_conflict = ToolResult(
                            status="error",
                            error=error_message,
                            content={
                                "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                                "credential_key": credential_key,
                                "conflicting_credential_key": other_key,
                                "sealbox_record_key": record_key,
                            },
                        )
                if post_write_conflict is None:
                    dropped_targets = (
                        _sealbox_delete_targets_for_credential(
                            latest_existing,
                            keep_keys=actual_new_keys,
                        )
                        if latest_existing is not None
                        else []
                    )
                    try:
                        _save_catalog_credential(
                            credential_key,
                            credential,
                            scope=resolved_scope,
                            dropped_sealbox_targets=dropped_targets,
                        )
                        _SECRET_CACHE.clear()
                    except Exception as exc:
                        save_error = exc
                    else:
                        failed_dropped = _delete_and_clear_dropped_targets(
                            dropped_targets,
                            scope=resolved_scope,
                            credential_key=credential_key,
                            event_name="credential_dropped_sealbox_delete_failed",
                        )
            if post_write_conflict is not None or save_error is not None:
                partial_write = False
                if write_source == "sealbox":
                    rollback_catalog = latest_catalog_for_rollback or CredentialCatalog()
                    partial_write = _rollback_new_sealbox_writes(
                        credential,
                        existing_keys=existing_keys,
                        catalog=rollback_catalog,
                        credential_key=credential_key,
                        scope=resolved_scope,
                    )
                if post_write_conflict is not None:
                    if partial_write:
                        post_write_conflict.content = {
                            **dict(post_write_conflict.content or {}),
                            "partial_store_write": True,
                        }
                    return post_write_conflict
                assert save_error is not None
                error_content: dict[str, Any] = {"credential_key": credential_key}
                if partial_write:
                    error_content["partial_store_write"] = True
                return ToolResult(
                    status="error",
                    error=f"Credential catalog save failed after writing to {write_source}: {save_error}",
                    content=error_content,
                )
        result_content: dict[str, Any] = {
            "status": "credential_stored",
            "credential_key": credential_key,
            "write_source": write_result.source,
            "item_id": write_result.item_id,
            "item_name": write_result.item_name,
            "metadata": _metadata_for_agent(credential_key, credential),
            "stored_secret_fields": sorted(secret_fields),
        }
        if credential.expires_at is not None:
            result_content["expires_at"] = credential.expires_at
        if failed_dropped:
            result_content["dropped_records_not_deleted"] = failed_dropped
            result_content["dropped_cleanup_retry_scheduled"] = True
        return ToolResult(status="success", content=result_content)
    except Exception as exc:
        return ToolResult(status="error", error=f"Credential collection failed: {exc}")


def register_existing_source(
    params: Mapping[str, Any],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
) -> ToolResult:
    try:
        resolved_scope = _owner_scope(context=context, scope=scope)
        raw_request = params.get("request")
        if params.get("expires_at") is not None or (
            isinstance(raw_request, Mapping) and raw_request.get("expires_at") is not None
        ):
            return ToolResult(
                status="error",
                error=(
                    "register_existing_source does not write secret values and cannot set an expiry "
                    "on an existing credential."
                ),
                content={"error_code": CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE},
            )
        request_payload = _normalize_existing_source_request_payload(dict(raw_request or params))
        credential_key = _credential_key(request_payload.get("credential_key"))
        precheck_catalog = load_catalog(scope=resolved_scope)
        precheck_existing = precheck_catalog.credentials.get(credential_key)
        if precheck_existing is not None and precheck_existing.expires_at is not None:
            return ToolResult(
                status="error",
                error=(
                    f"Cannot register an existing source over temporary credential {credential_key}; "
                    "temporary credentials can only be replaced or cleared through the secure broker form."
                ),
                content={
                    "error_code": CREDENTIAL_EXPIRY_FORM_ONLY,
                    "credential_key": credential_key,
                    "expires_at": precheck_existing.expires_at,
                },
            )
        field_specs = _normalize_field_specs(request_payload.get("fields") or [])
        if not field_specs:
            return ToolResult(
                status="error",
                error=(
                    "credential.register_existing_source requires at least one field spec. "
                    "For password-manager sources, pass field_name or fields plus source, item_id/item_name, and field_ref."
                ),
                content={
                    "error_code": "existing_source_fields_required",
                    "credential_key": credential_key,
                    "accepted_inline_shape": {
                        "credential_key": credential_key,
                        "field_name": "token",
                        "source": "bitwarden",
                        "item_name": "team-nats-agents :: default :: home_assistant_token",
                        "field_ref": "password",
                    },
                },
            )
        source_refs_by_field = _normalize_source_refs_by_field(request_payload)
        requested_sealbox_keys = {
            record_key
            for ref in source_refs_by_field.values()
            if (record_key := _sealbox_record_key(ref))
        }
        if precheck_existing is not None:
            precheck_dropped = _credential_sealbox_record_keys(precheck_existing) - requested_sealbox_keys
            if precheck_dropped:
                return _dropped_sealbox_records_form_only_result(credential_key, precheck_dropped)
        if _pending_dropped_sealbox_conflict(resolved_scope, requested_sealbox_keys) is not None:
            with _scope_catalog_lock(resolved_scope):
                precheck_catalog = load_catalog(scope=resolved_scope)
                pending_conflict = _resolve_pending_dropped_sealbox_conflict(
                    resolved_scope,
                    requested_sealbox_keys,
                    catalog=precheck_catalog,
                    credential_key=credential_key,
                )
            if pending_conflict is not None:
                return _pending_dropped_cleanup_conflict_result(credential_key, pending_conflict)
        shared_temp = _find_shared_sealbox_record(
            precheck_catalog,
            exclude_key=credential_key,
            target_keys=requested_sealbox_keys,
            temporary_only=True,
        )
        if shared_temp is not None:
            other_key, record_key = shared_temp
            return ToolResult(
                status="error",
                error=(
                    f"Cannot register {credential_key} against a Sealbox record owned by temporary "
                    f"credential {other_key}."
                ),
                content={
                    "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                    "credential_key": credential_key,
                    "conflicting_credential_key": other_key,
                    "sealbox_record_key": record_key,
                },
            )
        public_fields: dict[str, str] = {}
        catalog_fields: dict[str, CatalogCredentialField] = {}
        missing_fields: list[str] = []
        for field_name, field_spec in field_specs.items():
            kind = _string(field_spec.get("kind")) or "custom"
            if _field_is_secret(field_spec):
                source_ref = source_refs_by_field.get(field_name)
                if source_ref is None:
                    missing_fields.append(field_name)
                    continue
                resolved = _resolve_source_ref(source_ref, scope=resolved_scope)
                if isinstance(resolved, ToolResult) or not resolved:
                    return ToolResult(
                        status="error",
                        error=f"Existing credential source could not be resolved: {field_name}",
                        content={
                            "status": "credential_unavailable",
                            "error_code": "credential_unavailable",
                            "credential_key": credential_key,
                            "field_ref": field_name,
                            "source_ref": _redacted_source_ref(source_ref),
                        },
                    )
                catalog_fields[field_name] = CatalogCredentialField(
                    kind=kind,
                    visibility="secret",
                    value_source="existing_source",
                    resolution_order=[source_ref],
                )
                continue
            value = _string(field_spec.get("value"))
            if not value and bool(field_spec.get("required", True)):
                missing_fields.append(field_name)
                continue
            public_fields[field_name] = value
            catalog_fields[field_name] = CatalogCredentialField(
                kind=kind,
                visibility=_string(field_spec.get("visibility")) or "metadata_visible",
                value_source=_string(field_spec.get("value_source")) or "user_or_agent_supplied_metadata",
                value=value or None,
            )
        if missing_fields:
            return ToolResult(
                status="error",
                error=f"Missing required existing-source field metadata: {', '.join(sorted(missing_fields))}",
                content={"status": "credential_unavailable", "missing_fields": sorted(missing_fields)},
            )
        with credential_key_lock(resolved_scope, credential_key):
            with _scope_catalog_lock(resolved_scope):
                catalog = load_catalog(scope=resolved_scope)
                existing_credential = catalog.credentials.get(credential_key)
                if existing_credential is not None and existing_credential.expires_at is not None:
                    return ToolResult(
                        status="error",
                        error=(
                            f"Cannot register an existing source over temporary credential {credential_key}; "
                            "temporary credentials can only be replaced or cleared through the secure broker form."
                        ),
                        content={
                            "error_code": CREDENTIAL_EXPIRY_FORM_ONLY,
                            "credential_key": credential_key,
                            "expires_at": existing_credential.expires_at,
                        },
                    )
                pending_conflict = _resolve_pending_dropped_sealbox_conflict(
                    resolved_scope,
                    requested_sealbox_keys,
                    catalog=catalog,
                    credential_key=credential_key,
                )
                if pending_conflict is not None:
                    return _pending_dropped_cleanup_conflict_result(credential_key, pending_conflict)
                shared_temp = _find_shared_sealbox_record(
                    catalog,
                    exclude_key=credential_key,
                    target_keys=requested_sealbox_keys,
                    temporary_only=True,
                )
                if shared_temp is not None:
                    other_key, record_key = shared_temp
                    return ToolResult(
                        status="error",
                        error=(
                            f"Cannot register {credential_key} against a Sealbox record owned by temporary "
                            f"credential {other_key}."
                        ),
                        content={
                            "error_code": CREDENTIAL_EXPIRY_SHARED_RECORD,
                            "credential_key": credential_key,
                            "conflicting_credential_key": other_key,
                            "sealbox_record_key": record_key,
                        },
                    )
                now = utc_now_iso()
                credential = CatalogCredential(
                    display_name=_string(request_payload.get("display_name"))
                    or (existing_credential.display_name if existing_credential is not None else credential_key),
                    purpose=_string(request_payload.get("purpose"))
                    or (
                        existing_credential.purpose
                        if existing_credential is not None
                        else "Credential registered from an existing broker-local source."
                    ),
                    created_at=existing_credential.created_at if existing_credential is not None else now,
                    updated_at=now,
                    approval={
                        "status": "approved",
                        "approved_by": "user",
                        "approved_at": now,
                        "remember": True,
                        "source": "existing_source_registration",
                    },
                    fields=catalog_fields,
                )
                if existing_credential is not None:
                    actual_dropped_keys = (
                        _credential_sealbox_record_keys(existing_credential)
                        - _credential_sealbox_record_keys(credential)
                    )
                    if actual_dropped_keys:
                        return _dropped_sealbox_records_form_only_result(credential_key, actual_dropped_keys)
                _save_catalog_credential(credential_key, credential, scope=resolved_scope)
                _SECRET_CACHE.clear()
        result_content: dict[str, Any] = {
            "status": "credential_stored",
            "credential_key": credential_key,
            "metadata": _metadata_for_agent(credential_key, credential),
            "registered_source_fields": sorted(source_refs_by_field),
        }
        return ToolResult(status="success", content=result_content)
    except Exception as exc:
        return ToolResult(status="error", error=f"Existing-source credential registration failed: {exc}")


def _normalize_existing_source_request_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    if request_payload.get("fields"):
        return request_payload

    source_ref = _source_ref_from_inline_payload(request_payload)
    if source_ref is None:
        return request_payload

    field_name = (
        _string(request_payload.get("field_name"))
        or _string(request_payload.get("catalog_field"))
        or _string(request_payload.get("credential_field"))
        or _string(request_payload.get("field"))
        or _string(request_payload.get("field_ref"))
        or "value"
    )
    field_kind = _string(request_payload.get("kind")) or (
        "token" if "token" in field_name.lower() else "password" if field_name == "password" else "custom"
    )
    request_payload["fields"] = [
        {
            "name": field_name,
            "kind": field_kind,
            "visibility": "secret",
            "value_source": "existing_source",
            "source_ref": source_ref.model_dump(mode="json", exclude_none=True),
        }
    ]
    return request_payload


def _source_ref_from_inline_payload(payload: Mapping[str, Any]) -> CatalogSourceRef | None:
    env_var = _string(payload.get("env_var") or payload.get("source_env_var"))
    if env_var:
        return CatalogSourceRef(source="env", env_var=env_var, file_fallback=True)

    item_id = _string(payload.get("item_id") or payload.get("source_item_id"))
    item_name = _string(
        payload.get("item_name")
        or payload.get("source_item")
        or payload.get("source_item_name")
        or payload.get("bitwarden_item_name")
    )
    source = _string(
        payload.get("source")
        or payload.get("source_provider")
        or payload.get("credential_provider")
    )
    if not source and (item_id or item_name):
        source = "bitwarden"
    if not source:
        return None
    return CatalogSourceRef(
        source=source,
        item_id=item_id or None,
        item_name=item_name or None,
        field_ref=_string(payload.get("field_ref") or payload.get("source_field_ref") or payload.get("source_field"))
        or "password",
        vault=_string(payload.get("vault")) or None,
        folder=_string(payload.get("folder")) or None,
    )


def _normalize_source_refs_by_field(request_payload: Mapping[str, Any]) -> dict[str, CatalogSourceRef]:
    source_refs: dict[str, CatalogSourceRef] = {}
    raw_refs = request_payload.get("source_refs") or request_payload.get("resolution_order")
    if isinstance(raw_refs, Mapping):
        for field_name, raw_ref in raw_refs.items():
            if isinstance(raw_ref, Mapping):
                source_refs[str(field_name)] = CatalogSourceRef.model_validate(raw_ref)
    for field_name, field_spec in _normalize_field_specs(request_payload.get("fields") or []).items():
        if field_name in source_refs:
            continue
        raw_ref = field_spec.get("source_ref")
        if isinstance(raw_ref, Mapping):
            source_refs[field_name] = CatalogSourceRef.model_validate(raw_ref)
            continue
        raw_order = field_spec.get("resolution_order")
        if isinstance(raw_order, list) and raw_order and isinstance(raw_order[0], Mapping):
            source_refs[field_name] = CatalogSourceRef.model_validate(raw_order[0])
            continue
        env_var = _string(field_spec.get("env_var"))
        if env_var:
            source_refs[field_name] = CatalogSourceRef(source="env", env_var=env_var, file_fallback=True)
            continue
        inline_ref = _source_ref_from_inline_payload(field_spec)
        if inline_ref is not None:
            source_refs[field_name] = inline_ref
    return source_refs


def _normalize_field_specs(raw_fields: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw_fields, Mapping):
        iterable = [dict({"name": key}, **dict(value or {})) for key, value in raw_fields.items()]
    elif isinstance(raw_fields, list):
        iterable = [dict(item or {}) for item in raw_fields if isinstance(item, Mapping)]
    else:
        raise ValueError("fields must be a mapping or list of field specs.")
    fields: dict[str, dict[str, Any]] = {}
    for spec in iterable:
        name = _string(spec.get("name") or spec.get("field_ref"))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name):
            raise ValueError(f"Invalid credential field name: {name!r}")
        fields[name] = spec
    return fields


def write_credential_to_source(
    *,
    write_source: str,
    credential_key: str,
    request_payload: Mapping[str, Any],
    values: Mapping[str, str],
    field_specs: Mapping[str, Mapping[str, Any]],
    generated_secret_fields: set[str] | None = None,
    scope: CredentialOwnerScope,
    ttl_seconds: int | None = None,
) -> CredentialWriteResult | ToolResult:
    source = normalize_credential_source_name(write_source)
    if ttl_seconds is not None:
        if source != "sealbox":
            return ToolResult(
                status="error",
                error=(
                    "Temporary credentials with an expiry are only supported for Sealbox storage, "
                    f"not {source or '<empty>'}."
                ),
                content={
                    "error_code": CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE,
                    "write_source": source,
                    "credential_key": credential_key,
                },
            )
        if int(ttl_seconds) < 1:
            return ToolResult(
                status="error",
                error="Sealbox TTL must be at least 1 second.",
                content={"error_code": CREDENTIAL_EXPIRY_INVALID, "credential_key": credential_key},
            )
    if source == "bitwarden":
        return _write_bitwarden_login_item(
            credential_key=credential_key,
            request_payload=request_payload,
            values=values,
            field_specs=field_specs,
        )
    if source == "sealbox":
        return _write_sealbox_credential(
            credential_key=credential_key,
            request_payload=request_payload,
            values=values,
            field_specs=field_specs,
            generated_secret_fields=generated_secret_fields or set(),
            scope=scope,
            ttl_seconds=int(ttl_seconds) if ttl_seconds is not None else None,
        )
    return ToolResult(
        status="error",
        error=f"Credential writes are not implemented safely for source={source or '<empty>'}.",
        content={"error_code": "credential_write_source_unsupported", "write_source": source},
    )


def _catalog_item_name(credential_key: str, request_payload: Mapping[str, Any]) -> str:
    explicit = _string(request_payload.get("item_name"))
    if explicit:
        return explicit
    instance = _string(os.getenv("TEAM_INSTANCE_NAME") or os.getenv("INSTANCE_NAME")) or "default"
    return f"team-nats-agents :: {instance} :: {credential_key}"


def _sealbox_catalog_key(credential_key: str, request_payload: Mapping[str, Any]) -> str:
    explicit = _string(request_payload.get("item_id") or request_payload.get("sealbox_key"))
    if explicit:
        return safe_key_component(explicit, fallback="credential")
    instance = _string(os.getenv("TEAM_INSTANCE_NAME") or os.getenv("INSTANCE_NAME")) or "default"
    return ":".join(
        [
            "team-nats-agents",
            safe_key_component(instance, fallback="default"),
            safe_key_component(credential_key, fallback="credential"),
        ]
    )


def _secret_password_field_name(field_specs: Mapping[str, Mapping[str, Any]]) -> str | None:
    for field_name, spec in field_specs.items():
        if _string(spec.get("kind")).lower() == "password" and _field_is_secret(spec):
            return field_name
    return None


def _write_sealbox_credential(
    *,
    credential_key: str,
    request_payload: Mapping[str, Any],
    values: Mapping[str, str],
    field_specs: Mapping[str, Mapping[str, Any]],
    generated_secret_fields: set[str],
    scope: CredentialOwnerScope,
    ttl_seconds: int | None = None,
) -> CredentialWriteResult | ToolResult:
    tenant_env = sealbox_tenant_environment(
        scope_kind=scope.scope_kind,
        owner_id=scope.owner_id,
        provision=True,
    )
    if isinstance(tenant_env, ToolResult):
        return tenant_env
    item_key = _sealbox_catalog_key(credential_key, request_payload)
    # Pre-check every non-generated secret field before the first Sealbox write
    # so a refusal never leaves partial secrets behind.
    for field_name, spec in field_specs.items():
        if not _field_is_secret(spec) or field_name in generated_secret_fields:
            continue
        if not _string(values.get(field_name)):
            field_ref = safe_key_component(_field_storage_ref(field_name, spec), fallback=field_name)
            return ToolResult(
                status="error",
                error=f"Sealbox secret field '{item_key}:{field_ref}' resolved to an empty value.",
            )
    refs: dict[str, CatalogSourceRef] = {}
    username = _first_value_by_kind(values, field_specs, {"username", "email", "login"})
    password_field = _secret_password_field_name(field_specs)
    if password_field and username:
        result = _sealbox_set_credential(
            key=item_key,
            username=username,
            password=None if password_field in generated_secret_fields else values.get(password_field, ""),
            field_spec=field_specs.get(password_field, {}),
            generated=password_field in generated_secret_fields,
            env=tenant_env,
            ttl_seconds=ttl_seconds,
        )
        if isinstance(result, ToolResult):
            return result
        refs[password_field] = CatalogSourceRef(
            source="sealbox",
            item_id=item_key,
            item_name=item_key,
            field_ref="password",
        )
    elif password_field:
        field_key = f"{item_key}:{safe_key_component(_field_storage_ref(password_field, field_specs.get(password_field, {})))}"
        result = _sealbox_set_secret_field(
            key=field_key,
            value=values.get(password_field, ""),
            field_spec=field_specs.get(password_field, {}),
            generated=password_field in generated_secret_fields,
            env=tenant_env,
            ttl_seconds=ttl_seconds,
        )
        if isinstance(result, ToolResult):
            return result
        refs[password_field] = CatalogSourceRef(
            source="sealbox",
            item_id=field_key,
            item_name=field_key,
            field_ref="value",
        )
    for field_name, spec in field_specs.items():
        if field_name == password_field or not _field_is_secret(spec):
            continue
        field_ref = safe_key_component(_field_storage_ref(field_name, spec), fallback=field_name)
        field_key = f"{item_key}:{field_ref}"
        result = _sealbox_set_secret_field(
            key=field_key,
            value=values.get(field_name, ""),
            field_spec=spec,
            generated=field_name in generated_secret_fields,
            env=tenant_env,
            ttl_seconds=ttl_seconds,
        )
        if isinstance(result, ToolResult):
            return result
        refs[field_name] = CatalogSourceRef(
            source="sealbox",
            item_id=field_key,
            item_name=field_key,
            field_ref="value",
        )
    return CredentialWriteResult(source="sealbox", source_ref_by_field=refs, item_id=item_key, item_name=item_key)


def _sealbox_set_credential(
    *,
    key: str,
    username: str,
    password: str | None,
    field_spec: Mapping[str, Any],
    generated: bool,
    env: Mapping[str, str] | None = None,
    ttl_seconds: int | None = None,
) -> None | ToolResult:
    if not username.strip():
        return ToolResult(status="error", error="Sealbox credential writes require a non-empty username.")
    command = ["credential", "set", key, "--username", username, "--output", "json"]
    if ttl_seconds is not None:
        command.extend(["--ttl", str(int(ttl_seconds))])
    input_text: str | None = None
    if generated:
        command.append("--generate-password")
        command.extend(_sealbox_password_policy_args(field_spec, length=DEFAULT_PASSWORD_LENGTH))
    else:
        if not _string(password):
            return ToolResult(status="error", error="Sealbox credential writes require a non-empty password.")
        input_text = str(password)
    completed = run_sealbox(command, input_text=input_text, env=env, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = sealbox_redacted_completed_content(
        completed,
        action="credential.set",
        command=["sealbox-cli", *command],
        include_stdout=False,
    )
    if completed.returncode != 0:
        if sealbox_is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
            content["error_code"] = "auth_required"
        return ToolResult(status="error", content=content, error=f"sealbox-cli credential set exited with code {completed.returncode}")
    return None


def _sealbox_set_secret_field(
    *,
    key: str,
    value: str,
    field_spec: Mapping[str, Any],
    generated: bool,
    env: Mapping[str, str] | None = None,
    ttl_seconds: int | None = None,
) -> None | ToolResult:
    secret_value = value
    if generated:
        generated_value = _generate_password_with_sealbox(policy_args=_sealbox_password_policy_args(field_spec))
        if isinstance(generated_value, ToolResult):
            return generated_value
        secret_value = generated_value
    if not _string(secret_value):
        return ToolResult(status="error", error=f"Sealbox secret field {key!r} resolved to an empty value.")
    command = ["secret", "set", key, "--output", "json"]
    if ttl_seconds is not None:
        command.extend(["--ttl", str(int(ttl_seconds))])
    completed = run_sealbox(command, input_text=secret_value, env=env, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = sealbox_redacted_completed_content(
        completed,
        action="secret.set",
        command=["sealbox-cli", *command],
        include_stdout=False,
    )
    if completed.returncode != 0:
        if sealbox_is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
            content["error_code"] = "auth_required"
        return ToolResult(status="error", content=content, error=f"sealbox-cli secret set exited with code {completed.returncode}")
    return None


def _write_bitwarden_login_item(
    *,
    credential_key: str,
    request_payload: Mapping[str, Any],
    values: Mapping[str, str],
    field_specs: Mapping[str, Mapping[str, Any]],
) -> CredentialWriteResult | ToolResult:
    executable = shutil.which("bw")
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
    config_result = ensure_bitwarden_server_configured_from_env()
    if config_result is not None:
        return config_result
    item_name = _catalog_item_name(credential_key, request_payload)
    existing_item: Mapping[str, Any] | None = None
    item_id: str | None = None
    get_completed = _run_command([executable, "get", "item", item_name], timeout_seconds=30)
    if isinstance(get_completed, ToolResult):
        return get_completed
    if get_completed.returncode == 0:
        try:
            parsed = json.loads(get_completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult(status="error", error=f"bw get item returned non-JSON output: {exc}")
        if isinstance(parsed, Mapping):
            existing_item = parsed
            item_id = _string(parsed.get("id")) or None
    payload = _bitwarden_login_payload(
        item_name=item_name,
        request_payload=request_payload,
        values=values,
        field_specs=field_specs,
        existing_item=existing_item,
    )
    encoded = _bitwarden_encode_item(executable, payload)
    if isinstance(encoded, ToolResult):
        return encoded
    command = [executable, "edit", "item", item_id] if item_id else [executable, "create", "item"]
    completed = _run_command(command, input_text=encoded, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = {
        "provider": "credential_catalog",
        "write_source": "bitwarden",
        "credential_key": credential_key,
        "item_name": item_name,
        "exit_code": completed.returncode,
        "command": _command_preview(command),
    }
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"bw item write exited with code {completed.returncode}")
    if not item_id:
        try:
            created = json.loads(completed.stdout or "{}")
            if isinstance(created, Mapping):
                item_id = _string(created.get("id")) or None
        except json.JSONDecodeError:
            item_id = None
    refs: dict[str, CatalogSourceRef] = {}
    for field_name, spec in field_specs.items():
        if _field_is_secret(spec):
            refs[field_name] = CatalogSourceRef(
                source="bitwarden",
                item_id=item_id,
                item_name=item_name,
                field_ref=_field_storage_ref(field_name, spec),
            )
    return CredentialWriteResult(source="bitwarden", source_ref_by_field=refs, item_id=item_id, item_name=item_name)


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    stdin_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL} if input_text is None else {"input": input_text}
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
        return ToolResult(status="error", error=f"{Path(command[0]).name} command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"{Path(command[0]).name} command failed to start: {exc}")


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else list(command)


def _bitwarden_encode_item(executable: str, payload: Mapping[str, Any]) -> str | ToolResult:
    completed = _run_command([executable, "encode"], input_text=json.dumps(payload, separators=(",", ":")), timeout_seconds=15)
    if isinstance(completed, ToolResult):
        return completed
    if completed.returncode != 0:
        return ToolResult(
            status="error",
            content={"command": [executable, "encode"], "stderr": redact_text(completed.stderr or "").strip()},
            error=f"bw encode exited with code {completed.returncode}",
        )
    encoded = _string(completed.stdout)
    if not encoded:
        return ToolResult(status="error", error="bw encode returned empty output.")
    return encoded


def _bitwarden_login_payload(
    *,
    item_name: str,
    request_payload: Mapping[str, Any],
    values: Mapping[str, str],
    field_specs: Mapping[str, Mapping[str, Any]],
    existing_item: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(existing_item or {})
    payload["type"] = 1
    payload["name"] = item_name
    payload["notes"] = json.dumps(
        {
            "managed_by": "team-nats-agents secure credential catalog",
            "credential_key": request_payload.get("credential_key"),
            "purpose": request_payload.get("purpose"),
            "updated_at": utc_now_iso(),
        },
        sort_keys=True,
    )
    login = dict(payload.get("login") or {}) if isinstance(payload.get("login"), Mapping) else {}
    username = _first_value_by_kind(values, field_specs, {"username", "email", "login"})
    if username:
        login["username"] = username
    password_field = _first_secret_password_field(values, field_specs)
    if password_field:
        login["password"] = values[password_field]
    service_url = _string(request_payload.get("signup_url") or request_payload.get("service_url"))
    if service_url:
        login["uris"] = [{"uri": service_url}]
    else:
        login.setdefault("uris", [])
    payload["login"] = login
    custom_fields: list[dict[str, Any]] = []
    for raw in payload.get("fields", []):
        if isinstance(raw, Mapping) and _string(raw.get("name")) not in values:
            custom_fields.append(dict(raw))
    for field_name, value in values.items():
        if field_name == password_field:
            continue
        spec = field_specs.get(field_name, {})
        if _field_is_secret(spec):
            custom_fields.append({"name": field_name, "value": value, "type": 1})
        elif _field_is_metadata(spec):
            custom_fields.append({"name": field_name, "value": value, "type": 0})
    payload["fields"] = custom_fields
    return payload


def is_secret_credential_field(spec: Mapping[str, Any]) -> bool:
    """Return True when a credential field spec holds secret material (shared by form and storage)."""

    kind = _string(spec.get("kind")).lower()
    visibility = _string(spec.get("visibility")).lower()
    return bool(spec.get("secret", False)) or kind in SECRET_FIELD_KINDS or visibility == "secret"


def _field_is_secret(spec: Mapping[str, Any]) -> bool:
    return is_secret_credential_field(spec)


def _field_is_metadata(spec: Mapping[str, Any]) -> bool:
    return not _field_is_secret(spec)


def _field_storage_ref(field_name: str, spec: Mapping[str, Any]) -> str:
    kind = _string(spec.get("kind")).lower()
    field_ref = _string(spec.get("field_ref"))
    if field_ref:
        return field_ref
    if kind == "password" and field_name in {"password", "service_password"}:
        return "password"
    return field_name


def _first_value_by_kind(values: Mapping[str, str], field_specs: Mapping[str, Mapping[str, Any]], kinds: set[str]) -> str | None:
    for field_name, value in values.items():
        if _string(field_specs.get(field_name, {}).get("kind")).lower() in kinds and value:
            return value
    return None


def _first_secret_password_field(values: Mapping[str, str], field_specs: Mapping[str, Mapping[str, Any]]) -> str | None:
    for field_name, spec in field_specs.items():
        if _string(spec.get("kind")).lower() == "password" and values.get(field_name):
            return field_name
    return None


def build_catalog_credential(
    *,
    credential_key: str,
    request_payload: Mapping[str, Any],
    field_specs: Mapping[str, Mapping[str, Any]],
    public_fields: Mapping[str, str],
    write_result: CredentialWriteResult,
    expires_at: str | None = None,
) -> CatalogCredential:
    fields: dict[str, CatalogCredentialField] = {}
    for field_name, spec in field_specs.items():
        kind = _string(spec.get("kind")) or "custom"
        secret = _field_is_secret(spec)
        if secret:
            source_ref = write_result.source_ref_by_field.get(field_name)
            fields[field_name] = CatalogCredentialField(
                kind=kind,
                visibility="secret",
                value_source=_string(spec.get("value_source")) or "store_generated_or_user_submitted_secret",
                generation_policy=_string(spec.get("generation_policy")) or None,
                resolution_order=[source_ref] if source_ref else [],
            )
        else:
            fields[field_name] = CatalogCredentialField(
                kind=kind,
                visibility=_string(spec.get("visibility")) or "metadata_visible",
                value_source=_string(spec.get("value_source")) or "user_or_agent_supplied_metadata",
                value=public_fields.get(field_name) or _string(spec.get("value")) or None,
            )
    now = utc_now_iso()
    return CatalogCredential(
        display_name=_string(request_payload.get("display_name")) or credential_key,
        purpose=_string(request_payload.get("purpose")) or "Credential collected for approved agent workflow.",
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        approval={
            "status": "approved",
            "approved_by": "user",
            "approved_at": now,
            "remember": True,
        },
        fields=fields,
    )


def get_credential_metadata(
    credential_key: str,
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
    now: datetime | None = None,
) -> ToolResult:
    key = _credential_key(credential_key)
    catalog = load_catalog(scope=_owner_scope(context=context, scope=scope))
    credential = catalog.credentials.get(key)
    if credential is None:
        return ToolResult(
            status="error",
            error=f"Credential key is not registered: {key}",
            content={"error_code": "credential_unavailable", "credential_key": key},
        )
    if is_expired(credential.expires_at, now=now):
        return ToolResult(
            status="error",
            error=f"Temporary credential has expired: {key}",
            content={
                "error_code": CREDENTIAL_EXPIRED,
                "credential_key": key,
                "expires_at": credential.expires_at,
            },
        )
    return ToolResult(status="success", content={"credential": _metadata_for_agent(key, credential)})


def _sealbox_item_not_found(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return (
        "secret not found" in haystack
        or "credential not found" in haystack
        or "credential_not_found" in haystack
    )


def _sealbox_credential_delete_unsupported(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return (
        "unrecognized subcommand 'delete'" in haystack
        or 'unrecognized subcommand "delete"' in haystack
    )


def _sealbox_secret_delete_requires_version(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return (
        "secret delete --version" in haystack
        or ("required arguments" in haystack and "--version" in haystack)
        or ("required arguments were not provided" in haystack and "version" in haystack)
    )


def _sealbox_cli_update_required_result(*, content: Mapping[str, Any], command: list[str]) -> ToolResult:
    result_content = dict(content)
    result_content.update(
        {
            "error_code": "sealbox_cli_update_required",
            "required_behavior": "sealbox-cli secret delete <key> must delete all stored versions when --version is omitted.",
            "actual_command": ["sealbox-cli", *command],
            "user_action": "Rebuild or update Sealbox so the broker uses a current sealbox-cli binary.",
        }
    )
    return ToolResult(
        status="error",
        content=result_content,
        error=(
            "Installed sealbox-cli is too old for credential deletion. "
            "Current Sealbox supports `secret delete <key>` as an all-version delete; "
            "rebuild the Docker image with the latest Sealbox."
        ),
    )


def _sealbox_run_delete_command(
    *,
    item_key: str,
    item_kind: str,
    command: list[str],
    env: Mapping[str, str] | None = None,
) -> ToolResult | dict[str, Any] | subprocess.CompletedProcess[str]:
    completed = run_sealbox(command, env=env, timeout_seconds=30)
    if isinstance(completed, ToolResult):
        return completed
    content = sealbox_redacted_completed_content(
        completed,
        action=f"{item_kind}.delete",
        command=["sealbox-cli", *command],
    )
    if completed.returncode == 0:
        return completed
    if sealbox_is_auth_required(stdout=completed.stdout or "", stderr=completed.stderr or ""):
        content["error_code"] = "auth_required"
    if _sealbox_item_not_found(stdout=completed.stdout or "", stderr=completed.stderr or ""):
        return {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": item_kind,
            "already_absent": True,
            "result": content,
        }
    if command[:2] == ["credential", "delete"] and _sealbox_credential_delete_unsupported(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    ):
        return completed
    if command[:2] == ["secret", "delete"] and _sealbox_secret_delete_requires_version(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    ):
        return _sealbox_cli_update_required_result(content=content, command=command)
    command_name = " ".join(command[:2])
    return ToolResult(
        status="error",
        content=content,
        error=f"sealbox-cli {command_name} exited with code {completed.returncode}",
    )


def delete_sealbox_item(
    *,
    item_key: str,
    item_kind: str,
    env: Mapping[str, str] | None = None,
) -> ToolResult | dict[str, Any]:
    """Delete every version of one Sealbox item with ``env`` (the owner's tenant).

    A dict describes the deletion (``already_absent`` when the item was not
    there, which is not an error); a ToolResult is a failure, without values.
    Catalog credentials and the credential state
    (``credential_state_store.delete_credential_state``) both delete this way.
    """

    key = _string(item_key)
    if not key:
        return ToolResult(status="error", error="Sealbox item key is required for credential deletion.")
    invalid = sealbox_reject_unsupported_key(key)
    if invalid is not None:
        return invalid
    semantic_kind = "secret" if str(item_kind or "").strip().lower() == "secret" else "credential"
    command = ["secret", "delete", key] if semantic_kind == "secret" else ["credential", "delete", key]
    deleted = _sealbox_run_delete_command(
        item_key=key,
        item_kind=semantic_kind,
        command=command,
        env=env,
    )
    if isinstance(deleted, ToolResult) or isinstance(deleted, dict):
        return deleted
    if deleted.returncode != 0:
        if semantic_kind == "credential" and _sealbox_credential_delete_unsupported(
            stdout=deleted.stdout or "",
            stderr=deleted.stderr or "",
        ):
            fallback_command = ["secret", "delete", key]
            fallback_marker = "secret.delete_after_credential_delete_unsupported"
            fallback = _sealbox_run_delete_command(
                item_key=key,
                item_kind=semantic_kind,
                command=fallback_command,
                env=env,
            )
            if isinstance(fallback, ToolResult) or isinstance(fallback, dict):
                if isinstance(fallback, dict):
                    fallback["delete_command_fallback"] = fallback_marker
                return fallback
            return {
                "source": "sealbox",
                "item_key": key,
                "item_kind": semantic_kind,
                "already_absent": False,
                "delete_command_fallback": fallback_marker,
                "result": sealbox_redacted_completed_content(
                    fallback,
                    action=f"{semantic_kind}.delete",
                    command=["sealbox-cli", *fallback_command],
                ),
            }
        content = sealbox_redacted_completed_content(
            deleted,
            action=f"{semantic_kind}.delete",
            command=["sealbox-cli", *command],
        )
        return ToolResult(
            status="error",
            content=content,
            error=f"sealbox-cli {' '.join(command[:2])} exited with code {deleted.returncode}",
        )
    return {
        "source": "sealbox",
        "item_key": key,
        "item_kind": semantic_kind,
        "already_absent": False,
        "result": sealbox_redacted_completed_content(
            deleted,
            action=f"{semantic_kind}.delete",
            command=["sealbox-cli", *command],
        )
    }


def _sealbox_ref_kind(source_ref: CatalogSourceRef) -> str:
    return "credential" if _string(source_ref.field_ref).lower() in {"", "password"} else "secret"


def _explicit_direct_delete_ref(params: Mapping[str, Any], credential_key: str) -> CatalogSourceRef | ToolResult | None:
    has_explicit_item = any(
        _string(params.get(key))
        for key in (
            "source_item_id",
            "item_id",
            "sealbox_key",
            "source_item_name",
            "item_name",
        )
    )
    if not has_explicit_item:
        return None
    return _direct_delete_ref(params, credential_key)


def _direct_delete_ref(params: Mapping[str, Any], credential_key: str) -> CatalogSourceRef | ToolResult:
    source = normalize_credential_source_name(
        _string(params.get("source"))
        or _string(params.get("credential_source"))
        or _string(params.get("store_target"))
        or "sealbox"
    )
    if source == "default":
        source = "sealbox"
    if source != "sealbox":
        return ToolResult(
            status="error",
            error=f"Credential deletion is currently implemented for source=sealbox, not source={source or '<empty>'}.",
            content={"error_code": "credential_delete_source_unsupported", "source": source},
        )
    item_key = (
        _string(params.get("source_item_id"))
        or _string(params.get("item_id"))
        or _string(params.get("sealbox_key"))
        or _string(params.get("source_item_name"))
        or _string(params.get("item_name"))
        or credential_key
    )
    item_kind = _string(params.get("source_item_kind") or params.get("item_kind") or "credential").lower()
    field_ref = "value" if item_kind == "secret" else "password"
    return CatalogSourceRef(source="sealbox", item_id=item_key, item_name=item_key, field_ref=field_ref)


def _sealbox_delete_targets(refs: list[CatalogSourceRef]) -> list[tuple[str, str]]:
    target_kind_by_item: dict[str, str] = {}
    ordered_items: list[str] = []
    for source_ref in refs:
        item_key = _sealbox_record_key(source_ref) or _string(source_ref.item_id or source_ref.item_name)
        if not item_key:
            continue
        item_kind = _sealbox_ref_kind(source_ref)
        if item_key not in target_kind_by_item:
            ordered_items.append(item_key)
            target_kind_by_item[item_key] = item_kind
            continue
        if item_kind == "credential":
            target_kind_by_item[item_key] = "credential"
    return [(item_key, target_kind_by_item[item_key]) for item_key in ordered_items]


def delete_credential(
    params: Mapping[str, Any],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
    expected_updated_at: str | None = None,
    expected_expires_at: str | None = None,
    before_delete: Callable[[], ToolResult | None] | None = None,
    before_delete_credential: Callable[[CredentialOwnerScope, str, CatalogCredential], ToolResult | None] | None = None,
) -> ToolResult:
    try:
        resolved_scope = _owner_scope(context=context, scope=scope)
        credential_key = _credential_key(params.get("credential_key"))
        compare_and_delete = expected_updated_at is not None
        with credential_key_lock(resolved_scope, credential_key):
            store = CredentialCatalogStore(resolve_catalog_db_path())
            if compare_and_delete:
                current_row = store.load_credential(resolved_scope, credential_key)
                if current_row is None:
                    return ToolResult(
                        status="success",
                        content={
                            "status": "credential_absent",
                            "deleted": False,
                            "credential_key": credential_key,
                            "removed_catalog_entry": False,
                        },
                    )
                if (
                    current_row.updated_at != expected_updated_at
                    or current_row.credential.get("expires_at") != expected_expires_at
                ):
                    return ToolResult(
                        status="success",
                        content={
                            "status": "credential_changed",
                            "deleted": False,
                            "credential_key": credential_key,
                            "removed_catalog_entry": False,
                        },
                    )
                credential: CatalogCredential | None = CatalogCredential.model_validate(current_row.credential)
            else:
                catalog = load_catalog(scope=resolved_scope)
                credential = catalog.credentials.get(credential_key)
            refs: list[CatalogSourceRef] = []
            removed_catalog_entry = False
            if credential is not None:
                direct_ref = _explicit_direct_delete_ref(params, credential_key)
                if isinstance(direct_ref, ToolResult):
                    return direct_ref
                if direct_ref is not None:
                    refs.append(direct_ref)
                for field in credential.fields.values():
                    for source_ref in field.resolution_order:
                        if source_ref.source == "sealbox":
                            refs.append(source_ref)
                        elif source_ref.source != "env":
                            return ToolResult(
                                status="error",
                                error=(
                                    "Credential deletion is currently implemented for Sealbox-backed catalog fields only. "
                                    f"Unsupported source: {source_ref.source}."
                                ),
                                content={
                                    "error_code": "credential_delete_source_unsupported",
                                    "credential_key": credential_key,
                                    "source": source_ref.source,
                                },
                            )
                removed_catalog_entry = True
            else:
                direct_ref = _direct_delete_ref(params, credential_key)
                if isinstance(direct_ref, ToolResult):
                    return direct_ref
                refs.append(direct_ref)
            if not refs and not compare_and_delete:
                return ToolResult(
                    status="error",
                    error=f"Credential key has no Sealbox source refs to delete: {credential_key}",
                    content={
                        "error_code": "credential_delete_source_unavailable",
                        "credential_key": credential_key,
                    },
                )

            if before_delete is not None:
                # Runs under credential_key_lock after compare-and-delete verification
                # and before deleting Sealbox records or the catalog row so a hook
                # failure (e.g. link revocation or notice delivery) leaves the row for retry.
                hook_result = before_delete()
                if isinstance(hook_result, ToolResult):
                    return hook_result
            elif before_delete_credential is not None and credential is not None:
                hook_result = before_delete_credential(resolved_scope, credential_key, credential)
                if isinstance(hook_result, ToolResult):
                    return hook_result

            deleted_refs: list[dict[str, Any]] = []
            if refs:
                tenant_env = sealbox_tenant_environment(
                    scope_kind=resolved_scope.scope_kind,
                    owner_id=resolved_scope.owner_id,
                    provision=False,
                )
                if isinstance(tenant_env, ToolResult):
                    return tenant_env
                for item_key, item_kind in _sealbox_delete_targets(refs):
                    deleted = delete_sealbox_item(item_key=item_key, item_kind=item_kind, env=tenant_env)
                    if isinstance(deleted, ToolResult):
                        return deleted
                    deleted_ref = {
                        "source": deleted["source"],
                        "item_key": deleted["item_key"],
                        "item_kind": deleted["item_kind"],
                    }
                    if deleted.get("already_absent"):
                        deleted_ref["already_absent"] = True
                    if deleted.get("delete_command_fallback"):
                        deleted_ref["delete_command_fallback"] = deleted["delete_command_fallback"]
                    deleted_refs.append(deleted_ref)

            if removed_catalog_entry:
                if compare_and_delete:
                    assert expected_updated_at is not None
                    deleted_row = store.delete_credential_if_unchanged(
                        resolved_scope,
                        credential_key,
                        expected_updated_at=expected_updated_at,
                        expected_expires_at=expected_expires_at,
                    )
                    if not deleted_row:
                        _SECRET_CACHE.clear()
                        return ToolResult(
                            status="success",
                            content={
                                "status": "credential_changed",
                                "deleted": False,
                                "credential_key": credential_key,
                                "removed_catalog_entry": False,
                            },
                        )
                else:
                    _delete_catalog_credential(credential_key, scope=resolved_scope)
            _SECRET_CACHE.clear()
            return ToolResult(
                status="success",
                content={
                    "status": "credential_deleted",
                    "deleted": True,
                    "credential_key": credential_key,
                    "removed_catalog_entry": removed_catalog_entry,
                    "deleted_source_refs": deleted_refs,
                    "message": f"Credential {credential_key} was deleted.",
                },
            )
    except Exception as exc:
        return ToolResult(status="error", error=f"Credential deletion failed: {exc}")


def _openshell_refusal(code: str, message: str, **details: Any) -> ToolResult:
    """A refused OpenShell binding, logged with identifiers only (never values)."""

    _LOGGER.warning(
        "event=openshell_binding_refused error_code=%s tool_id=%s account_link_id=%s",
        code,
        details.get("tool_id"),
        details.get("account_link_id"),
    )
    return ToolResult(status="error", error=message, content={"error_code": code, **details})


def bind_for_openshell(
    params: Mapping[str, Any],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
) -> ToolResult:
    """Give code in an OpenShell sandbox a linked account's environment values for one use.

    ``params["tool_id"]`` names the tool (for example a web API tool,
    ``http:*``) whose linked account the code should use. The broker's gate
    has already decided access to that tool (the ``linked_tool`` catalog mode
    requires full access) and chosen its account link: the person's own, or
    else the team's. This puts only the link's ``env`` injections into a new
    OpenShell provider, attaches it to the sandbox, and returns the variable
    names, never the values. ``credential.release_openshell_binding`` detaches
    and deletes the provider.
    """

    del scope  # the account link, not the caller, chooses whose credential is used
    tool_id = _string(params.get("tool_id"))
    if not tool_id:
        return _openshell_refusal(
            OPENSHELL_TOOL_ID_REQUIRED,
            "credential.bind_for_openshell needs tool_id: the tool whose linked account the code should use.",
        )
    provider_type = _string(params.get("provider_type")) or OPENSHELL_DEFAULT_PROVIDER_TYPE
    if not _OPENSHELL_PROVIDER_TYPE_PATTERN.fullmatch(provider_type):
        return _openshell_refusal(
            OPENSHELL_PROVIDER_TYPE_INVALID,
            "provider_type uses lowercase letters, digits, '_' and '-' (at most 64 characters), "
            f"for example '{OPENSHELL_DEFAULT_PROVIDER_TYPE}'.",
            tool_id=tool_id,
        )
    # Each person's code runs in their own sandbox (runtime_settings.openshell_naming),
    # so the broker names it from the verified person, never from the request.
    person_id = context.canonical_user_id if context is not None else None
    sandbox_name = openshell_sandbox_name(os.getenv(OPENSHELL_ENV_SANDBOX_NAME), person_id)
    requested = _string(params.get("sandbox_name"))
    if requested and requested != sandbox_name:
        return _openshell_refusal(
            OPENSHELL_SANDBOX_NOT_OURS,
            f"Code for this task runs in the sandbox {sandbox_name}; the broker gives accounts only to it.",
            tool_id=tool_id,
        )
    link = context.account_link if context is not None else None
    if context is None or link is None:
        return _openshell_refusal(
            REASON_ACCOUNT_LINK_MISSING,
            f"No account is linked for {tool_id}, so the broker has no credential to give the sandbox.",
            tool_id=tool_id,
            next_step=(
                f"Ask TeamLead to request an account link for {tool_id}: a team administrator "
                "links the team's account, or the person links their own."
            ),
        )
    if link.tool_id != tool_id:
        return _openshell_refusal(
            ACCOUNT_LINK_TOOL_MISMATCH,
            f"The broker chose the account linked to {link.tool_id}, not {tool_id}, so nothing was bound.",
            tool_id=tool_id,
            account_link_id=link.link_id,
        )
    if not any(item.placement is InjectionPlacement.ENV for item in link.injections):
        return _openshell_refusal(
            ACCOUNT_LINK_HAS_NO_ENVIRONMENT,
            f"The account linked to {tool_id} names no environment variables, so there is nothing to give the sandbox.",
            tool_id=tool_id,
            account_link_id=link.link_id,
            next_step="Link the account again and name the environment variables code should see.",
        )
    resolved = _resolve_injections(link, frozenset({InjectionPlacement.ENV}))
    if isinstance(resolved, ToolResult):
        _LOGGER.warning(
            "event=openshell_binding_failed stage=credential tool_id=%s account_link_id=%s error_code=%s",
            tool_id,
            link.link_id,
            dict(resolved.content or {}).get("error_code"),
        )
        return resolved
    values, secret_values = resolved
    # _resolve_injections lists the rendered value of every secret field, so a
    # value in that list came from a secret; the rest are public metadata.
    secret_set = set(secret_values)
    secret_env = {name: value for (_, name), value in values.items() if value in secret_set}
    public_env = {name: value for (_, name), value in values.items() if value not in secret_set}
    lease_id = "lease_" + secrets.token_urlsafe(12).replace("-", "_")
    provider_name = f"{_safe_provider_name(tool_id)}-{lease_id[-8:]}"
    result = _create_and_attach_openshell_provider(
        provider_name=provider_name,
        provider_type=provider_type,
        sandbox_name=sandbox_name,
        secret_env=secret_env,
        public_env=public_env,
    )
    if isinstance(result, ToolResult):
        _LOGGER.warning(
            "event=openshell_binding_failed stage=provider tool_id=%s account_link_id=%s sandbox_name=%s",
            tool_id,
            link.link_id,
            sandbox_name,
        )
        return result
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_default_openshell_binding_ttl_seconds())
    person_id = _string(context.canonical_user_id) or None
    _LEASES[lease_id] = {
        "provider_name": provider_name,
        "sandbox_name": sandbox_name,
        "expires_at": expires_at.isoformat(),
        "tool_id": tool_id,
        "link_id": link.link_id,
        "person_id": person_id,
    }
    env_names = sorted({*secret_env, *public_env})
    _LOGGER.info(
        "event=openshell_binding_ready tool_id=%s account_link_id=%s lease_id=%s sandbox_name=%s env_count=%s",
        tool_id,
        link.link_id,
        lease_id,
        sandbox_name,
        len(env_names),
    )
    return ToolResult(
        status="success",
        content={
            "status": "openshell_binding_ready",
            "tool_id": tool_id,
            "account_link_id": link.link_id,
            "lease_id": lease_id,
            "provider_name": provider_name,
            "provider_type": provider_type,
            "sandbox_name": sandbox_name,
            "expires_at": expires_at.isoformat(),
            "env": env_names,
        },
    )


def _safe_provider_name(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return rendered[:48] or "team-credential"


def _create_and_attach_openshell_provider(
    *,
    provider_name: str,
    provider_type: str,
    sandbox_name: str,
    secret_env: Mapping[str, str],
    public_env: Mapping[str, str],
) -> ToolResult | None:
    executable = shutil.which("openshell")
    if not executable:
        return ToolResult(status="error", error="OpenShell CLI is not installed or not on PATH.")
    env = dict(os.environ)
    env.update(secret_env)
    env.update(public_env)
    create_command = [
        executable,
        "provider",
        "create",
        "--name",
        provider_name,
        "--type",
        provider_type,
    ]
    for env_name in sorted({*secret_env, *public_env}):
        create_command.extend(["--credential", env_name])
    create_completed = _run_command(create_command, env=env, timeout_seconds=30)
    if isinstance(create_completed, ToolResult):
        return create_completed
    if create_completed.returncode != 0:
        return ToolResult(
            status="error",
            content={
                "command": _command_preview(create_command),
                "stderr": redact_text(create_completed.stderr or create_completed.stdout or "").strip(),
            },
            error=f"openshell provider create exited with code {create_completed.returncode}",
        )
    attach_command = [executable, "sandbox", "provider", "attach", sandbox_name, provider_name]
    attach_completed = _run_command(attach_command, timeout_seconds=30)
    if isinstance(attach_completed, ToolResult):
        return attach_completed
    if attach_completed.returncode != 0:
        _run_command([executable, "provider", "delete", provider_name], timeout_seconds=15)
        return ToolResult(
            status="error",
            content={
                "command": _command_preview(attach_command),
                "stderr": redact_text(attach_completed.stderr or attach_completed.stdout or "").strip(),
            },
            error=f"openshell sandbox provider attach exited with code {attach_completed.returncode}",
        )
    return None


def release_openshell_binding(
    params: Mapping[str, Any],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
) -> ToolResult:
    """Detach and delete the OpenShell provider of a lease made for the same person.

    A lease made for another person reads as not active, so a caller cannot
    learn about or end someone else's lease.
    """

    del scope  # a lease belongs to the person it was made for, not an owner scope
    lease_id = _string(params.get("lease_id"))
    lease = _LEASES.get(lease_id)
    person_id = _string(context.canonical_user_id) if context is not None else ""
    if lease is None or _string(lease.get("person_id")) != person_id:
        if lease is not None:
            _LOGGER.warning(
                "event=openshell_lease_release_refused reason=other_person lease_id=%s tool_id=%s",
                lease_id,
                lease.get("tool_id"),
            )
        return ToolResult(
            status="error",
            error=f"OpenShell credential lease is not active: {lease_id}",
            content={"error_code": "lease_not_found", "lease_id": lease_id},
        )
    _LEASES.pop(lease_id, None)
    executable = shutil.which("openshell")
    if not executable:
        return ToolResult(status="error", error="OpenShell CLI is not installed or not on PATH.")
    provider_name = str(lease["provider_name"])
    sandbox_name = str(lease["sandbox_name"])
    detach = _run_command([executable, "sandbox", "provider", "detach", sandbox_name, provider_name], timeout_seconds=15)
    delete = _run_command([executable, "provider", "delete", provider_name], timeout_seconds=15)
    content = {
        "status": "openshell_binding_released",
        "lease_id": lease_id,
        "provider_name": provider_name,
        "sandbox_name": sandbox_name,
    }
    errors = []
    for phase, result in (("detach", detach), ("delete", delete)):
        if isinstance(result, ToolResult):
            errors.append(f"{phase}: {result.error}")
        elif result.returncode != 0:
            errors.append(f"{phase}: exited with code {result.returncode}")
    if errors:
        return ToolResult(status="error", content=content, error="; ".join(errors))
    return ToolResult(status="success", content=content)


def _linked_account_scope(link: AccountLinkV1) -> CredentialOwnerScope:
    if link.owner_kind is LinkOwnerKind.TEAM:
        return CredentialOwnerScope.team(link.owner_id)
    return CredentialOwnerScope.user(link.owner_id)


def linked_credential_state(
    link: AccountLinkV1,
    *,
    now: datetime | None = None,
) -> Literal["usable", "missing", "expired"]:
    """Inspect whether ``link`` points at a present, unexpired catalog row (read-only).

    Reads only the single linked catalog row read-only so checking an account
    link never creates a catalog file or decodes the owner's other entries.
    A missing catalog row counts as ``"missing"`` so broken links never block
    team fallback or claim a shared fallback that is not stored.
    """

    raw = load_credential_readonly(resolve_catalog_db_path(), _linked_account_scope(link), link.credential_key)
    if not isinstance(raw, Mapping):
        return "missing"
    raw_expires = raw.get("expires_at")
    if raw_expires is not None and not (isinstance(raw_expires, str) and not raw_expires.strip()):
        if is_expired(raw_expires, now=now):
            return "expired"
    return "usable"


def linked_credential_expired(
    link: AccountLinkV1,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when ``link`` points at a temporary credential whose ``expires_at`` has passed."""

    return linked_credential_state(link, now=now) == "expired"


def _resolve_injections(
    link: AccountLinkV1,
    placements: frozenset[InjectionPlacement],
    *,
    now: datetime | None = None,
) -> tuple[dict[tuple[InjectionPlacement, str], str], list[str]] | ToolResult:
    """Rendered values for the link's injections at ``placements``, and the
    secret values to redact from anything returned."""

    scope = _linked_account_scope(link)
    catalog = load_catalog(scope=scope)
    values: dict[tuple[InjectionPlacement, str], str] = {}
    secrets_found: list[str] = []
    for injection in link.injections:
        if injection.placement not in placements:
            continue
        resolved = resolve_secret_field(
            link.credential_key,
            injection.field,
            catalog=catalog,
            scope=scope,
            now=now,
        )
        if isinstance(resolved, ToolResult):
            if str((resolved.content or {}).get("error_code") or "") == CREDENTIAL_EXPIRED:
                content: dict[str, Any] = {
                    "error_code": REASON_ACCOUNT_LINK_CREDENTIAL_EXPIRED,
                    "credential_key": link.credential_key,
                    "account_link_id": link.link_id,
                    "tool_id": link.tool_id,
                }
                if resolved.content and resolved.content.get("expires_at"):
                    content["expires_at"] = resolved.content["expires_at"]
                return ToolResult(
                    status="error",
                    error=(
                        f"The temporary credential linked to {link.tool_id} has expired "
                        "and can no longer be used."
                    ),
                    content=content,
                )
            return resolved
        rendered = injection.render(resolved.value)
        values[(injection.placement, injection.name)] = rendered
        # Team gate credentials are always redacted from responses and errors,
        # even when stored in a field not marked secret.
        if resolved.value and (resolved.secret or link.purpose is LinkPurpose.GATE):
            secrets_found += [resolved.value, rendered]
    return values, secrets_found


class _ReservedQueryError(ValueError):
    """Raised when an agent-supplied query parameter collides with a broker-managed injection."""

    def __init__(self, message: str, *, query_params: list[str], source: str) -> None:
        super().__init__(message)
        self.query_params = query_params
        self.source = source


def _apply_api_injections(
    headers: dict[str, str],
    *,
    account_link: AccountLinkV1 | None,
    gate_link: AccountLinkV1 | None,
    host: str,
) -> tuple[dict[str, str], dict[str, str], list[str]] | ToolResult:
    """Resolve account and gate injections, refuse reserved header overrides or overlaps, and return
    ``(account_query, gate_query, secret_values)``."""

    placements = frozenset({InjectionPlacement.HEADER, InjectionPlacement.QUERY})
    agent_headers_lower = {name.lower() for name in headers}
    injected_headers_by_lower: dict[str, tuple[str, str]] = {}
    account_query: dict[str, str] = {}
    gate_query: dict[str, str] = {}
    secret_values: list[str] = []

    sources: tuple[tuple[str, str, AccountLinkV1 | None], ...] = (
        ("account", "linked account", account_link),
        ("gate", "team gate credential", gate_link),
    )
    for source_tag, source_label, link in sources:
        if link is None:
            continue
        resolved = _resolve_injections(link, placements)
        if isinstance(resolved, ToolResult):
            return resolved
        injected, secrets_found = resolved
        secret_values.extend(secrets_found)
        link_headers = {name: value for (place, name), value in injected.items() if place is InjectionPlacement.HEADER}
        link_query = {name: value for (place, name), value in injected.items() if place is InjectionPlacement.QUERY}
        reserved = {name.lower() for name in link_headers} & agent_headers_lower
        if reserved:
            reserved_sorted = sorted(reserved)
            _LOGGER.warning(
                "Refused web request that set a broker-managed header. "
                "event=api_request_header_reserved source=%s host=%s link_id=%s headers=%s",
                source_tag,
                host,
                link.link_id,
                ",".join(reserved_sorted),
            )
            return ToolResult(
                status="error",
                error=f"The broker sets these headers from the {source_label}: {', '.join(reserved_sorted)}.",
                content={"error_code": "api_request_header_reserved", "headers": reserved_sorted},
            )
        header_collisions = sorted(name.lower() for name in link_headers if name.lower() in injected_headers_by_lower)
        if header_collisions:
            _LOGGER.warning(
                "Refused web request because account and gate links set the same header. "
                "event=api_request_injection_collision placement=header host=%s headers=%s",
                host,
                ",".join(header_collisions),
            )
            return ToolResult(
                status="error",
                error=(
                    "The linked account and team gate credential both set the same headers: "
                    f"{', '.join(header_collisions)}."
                ),
                content={"error_code": "api_request_injection_collision", "headers": header_collisions},
            )
        for name, value in link_headers.items():
            injected_headers_by_lower[name.lower()] = (name, value)
        if source_tag == "account":
            account_query = link_query
        else:
            query_collisions = sorted(set(account_query) & set(link_query))
            if query_collisions:
                _LOGGER.warning(
                    "Refused web request because account and gate links set the same query parameter. "
                    "event=api_request_injection_collision placement=query host=%s params=%s",
                    host,
                    ",".join(query_collisions),
                )
                return ToolResult(
                    status="error",
                    error=(
                        "The linked account and team gate credential both set the same query values: "
                        f"{', '.join(query_collisions)}."
                    ),
                    content={"error_code": "api_request_injection_collision", "query_params": query_collisions},
                )
            gate_query = link_query

    for name, value in injected_headers_by_lower.values():
        headers[name] = value
    return account_query, gate_query, secret_values


def api_request(
    params: Mapping[str, Any],
    *,
    context: ToolContext | None = None,
    scope: CredentialOwnerScope | None = None,
) -> ToolResult:
    """Send one web request for a web API tool (``http:*``).

    The broker's gate already matched the host to the tool, decided access, and
    chose the account link and any team gate link; this adds both credentials
    where their links say (headers or query values) and redacts every secret
    value from the response and any error. Requests carry no credential
    placeholders.
    """

    del scope  # the account and gate links, not the caller, choose whose credentials are used
    secret_values: list[str] = []
    try:
        method = _string(params.get("method") or "GET").upper()
        if method not in _ALLOWED_API_METHODS:
            return ToolResult(
                status="error",
                error=f"Unsupported API request method: {method}",
                content={"error_code": "api_request_method_unsupported", "method": method},
            )
        url = _string(params.get("url") or params.get("endpoint_url"))
        if not url:
            return ToolResult(status="error", error="credential.api_request requires url.")
        parsed_url = urllib_parse.urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            return ToolResult(
                status="error",
                error="credential.api_request url must be an absolute http(s) URL.",
                content={"error_code": "api_request_url_invalid"},
            )
        host = parsed_url.hostname.lower()
        raw_headers = params.get("headers") or {}
        json_payload = params.get("json") if "json" in params else params.get("json_body")
        raw_body = params.get("body")
        raw_query = params.get("params") or params.get("query")
        if any(
            _contains_credential_placeholder(value)
            for value in (url, raw_headers, json_payload, raw_body, raw_query)
        ):
            return ToolResult(
                status="error",
                error=(
                    "Requests carry no credential placeholders: the broker adds the credential of the "
                    "account linked to this web API tool."
                ),
                content={"error_code": "api_request_placeholders_unsupported"},
            )
        headers = _api_headers(raw_headers)
        link = context.account_link if context is not None else None
        gate_link = context.gate_link if context is not None else None
        applied = _apply_api_injections(headers, account_link=link, gate_link=gate_link, host=host)
        if isinstance(applied, ToolResult):
            return applied
        account_query, gate_query, secret_values = applied
        if not any(name.lower() == "user-agent" for name in headers):
            user_agent = (
                context.api_request_user_agent
                if context is not None and context.api_request_user_agent
                else DEFAULT_API_REQUEST_USER_AGENT
            )
            headers["User-Agent"] = user_agent
        body_bytes: bytes | None = None
        if json_payload is not None:
            body_bytes = json.dumps(json_payload).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif raw_body is not None:
            body_bytes = _api_body_to_bytes(raw_body)
        if body_bytes is not None and method in {"GET", "HEAD"}:
            return ToolResult(
                status="error",
                error=f"credential.api_request does not send request bodies with {method}.",
                content={"error_code": "api_request_body_method_invalid", "method": method},
            )
        try:
            request_url = _url_with_query_params(url, raw_query, account_query, gate_injected=gate_query)
        except _ReservedQueryError as exc:
            _LOGGER.warning(
                "Refused web request that set a broker-managed query parameter. "
                "event=api_request_query_reserved source=%s host=%s params=%s",
                exc.source,
                host,
                ",".join(exc.query_params),
            )
            return ToolResult(
                status="error",
                error=str(exc),
                content={"error_code": "api_request_query_reserved", "query_params": exc.query_params},
            )
        timeout_seconds = _bounded_api_timeout(params.get("timeout_seconds") or params.get("timeout") or 30)
        max_bytes = _bounded_response_bytes(params.get("max_response_bytes"))
        request = urllib_request.Request(request_url, data=body_bytes, headers=headers, method=method)
        link_id = link.link_id if link is not None else ""
        gate_link_id = gate_link.link_id if gate_link is not None else ""
        try:
            response = _send_api_request(request, timeout=timeout_seconds)
        except urllib_error.HTTPError as exc:
            return _api_response_result(
                response=exc,
                link_id=link_id,
                gate_link_id=gate_link_id,
                method=method,
                host=host,
                secret_values=secret_values,
                max_bytes=max_bytes,
            )
        except urllib_error.URLError as exc:
            redacted = redact_text(_redact_known_secret_text(str(exc), secret_values))
            return ToolResult(
                status="error",
                error=f"credential.api_request failed: {redacted}",
                content={"error_code": "api_request_failed", "host": host},
            )
        try:
            return _api_response_result(
                response=response,
                link_id=link_id,
                gate_link_id=gate_link_id,
                method=method,
                host=host,
                secret_values=secret_values,
                max_bytes=max_bytes,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        redacted = redact_text(_redact_known_secret_text(str(exc), secret_values))
        return ToolResult(status="error", error=f"credential.api_request failed: {redacted}")


class _ReturnRedirects(urllib_request.HTTPRedirectHandler):
    """Return 3xx responses instead of following them.

    urllib forwards every non-content header, including injected credentials,
    to the redirect target, which would bypass the broker's check that the
    host belongs to the web API tool. The caller sees the status and Location
    and can make a new, checked request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_NO_REDIRECT_OPENER = urllib_request.build_opener(_ReturnRedirects)


def _send_api_request(request: urllib_request.Request, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _api_headers(raw_headers: Any) -> dict[str, str]:
    if not raw_headers:
        return {}
    if not isinstance(raw_headers, Mapping):
        raise ValueError("credential.api_request headers must be a mapping.")
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = _string(raw_name)
        value = _string(raw_value)
        if not name or "\n" in name or "\r" in name:
            raise ValueError("credential.api_request header names must be non-empty single-line strings.")
        if "\n" in value or "\r" in value:
            raise ValueError("credential.api_request header values must be single-line strings.")
        headers[name] = value
    return headers


def _contains_credential_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return _PLACEHOLDER_PATTERN.search(value) is not None
    if isinstance(value, Mapping):
        return any(_contains_credential_placeholder(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_credential_placeholder(item) for item in value)
    return False


def _url_with_query_params(
    url: str,
    raw_params: Any,
    injected: Mapping[str, str] | None = None,
    *,
    gate_injected: Mapping[str, str] | None = None,
) -> str:
    if not raw_params and not injected and not gate_injected:
        return url
    parsed = urllib_parse.urlparse(url)
    existing = urllib_parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not raw_params:
        query_items: list[Any] = []
    elif isinstance(raw_params, Mapping):
        query_items = list(raw_params.items())
    elif isinstance(raw_params, list):
        query_items = raw_params
    else:
        raise ValueError("secure request params/query must be a mapping or list of key/value pairs.")
    account_map = dict(injected or {})
    gate_map = dict(gate_injected or {})
    supplied = {str(item[0]) for item in [*existing, *query_items]}
    account_overlap = sorted(supplied & set(account_map))
    if account_overlap:
        raise _ReservedQueryError(
            f"The broker sets these query values from the linked account: {', '.join(account_overlap)}.",
            query_params=account_overlap,
            source="account",
        )
    gate_overlap = sorted(supplied & set(gate_map))
    if gate_overlap:
        raise _ReservedQueryError(
            f"The broker sets these query values from the team gate credential: {', '.join(gate_overlap)}.",
            query_params=gate_overlap,
            source="gate",
        )
    query = urllib_parse.urlencode([*existing, *query_items, *account_map.items(), *gate_map.items()], doseq=True)
    return urllib_parse.urlunparse(parsed._replace(query=query))


def _api_body_to_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, (dict, list)):
        return json.dumps(body).encode("utf-8")
    return str(body).encode("utf-8")


def _bounded_api_timeout(value: Any) -> int:
    try:
        return max(1, min(int(value), 120))
    except (TypeError, ValueError):
        return 30


# How much of a response a web request returns: 1 MB unless the caller asks for
# more, and never more than 16 MB (a screenshot comes back as base64 in JSON).
_DEFAULT_API_RESPONSE_BYTES = 1_000_000
_MAX_API_RESPONSE_BYTES = 16_000_000


def _bounded_response_bytes(value: Any) -> int:
    try:
        return max(1, min(int(value), _MAX_API_RESPONSE_BYTES))
    except (TypeError, ValueError):
        return _DEFAULT_API_RESPONSE_BYTES


# Minimum lookahead past max_bytes when a response is truncated, so pattern-matched
# secrets (such as JWTs or API keys) that cross the byte cut are redacted completely.
_REDACTION_LOOKAHEAD_MIN_BYTES = 1024
_PERCENT_HEX_RE = re.compile(r"%[0-9A-F]{2}")


def _api_response_result(
    *,
    response: Any,
    link_id: str,
    gate_link_id: str = "",
    method: str,
    host: str,
    secret_values: list[str],
    max_bytes: int = _DEFAULT_API_RESPONSE_BYTES,
) -> ToolResult:
    status_code = int(getattr(response, "status", None) or getattr(response, "code", None) or response.getcode())
    headers = getattr(response, "headers", {}) or {}
    content_type = _string(headers.get("Content-Type") if hasattr(headers, "get") else "")
    charset = "utf-8"
    get_charset = getattr(headers, "get_content_charset", None)
    if callable(get_charset):
        charset = get_charset() or charset
    text, truncated, already_redacted = _read_api_response_text(
        response,
        charset=charset,
        max_bytes=max_bytes,
        secret_values=secret_values,
    )
    content: dict[str, Any] = {
        "status": "api_request_completed",
        "method": method,
        "host": host,
        "http_status": status_code,
        "content_type": content_type,
        "truncated": truncated,
    }
    if link_id:
        content["account_link_id"] = link_id
    if gate_link_id:
        content["gate_link_id"] = gate_link_id
    location = _string(headers.get("Location") if hasattr(headers, "get") else "")
    if 300 <= status_code < 400 and location:
        # Redirects are not followed (credentials must not reach another host);
        # the caller may make a new request, which is checked again.
        content["redirect_location"] = redact_text(_redact_known_secret_text(location, secret_values))
    parsed_json: Any | None = None
    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            parsed_json = json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            parsed_json = None
    if parsed_json is not None:
        content["json"] = redact_json(_redact_known_secret_values(parsed_json, secret_values))
    elif already_redacted:
        content["text"] = text
    else:
        content["text"] = redact_text(_redact_known_secret_text(text, secret_values))
    return ToolResult(status="success", content=content)


def _expand_secret_variants(secret_values: list[str]) -> tuple[str, ...]:
    """Raw, URL-encoded (``quote_plus``, ``quote``, lowercase percent-hex), and JSON-escaped forms to redact."""

    variants: set[str] = set()
    for raw in secret_values:
        if not raw:
            continue
        candidates = [
            raw,
            urllib_parse.quote_plus(raw),
            urllib_parse.quote(raw, safe=""),
            urllib_parse.quote(raw),
            json.dumps(raw)[1:-1],
        ]
        for candidate in candidates:
            if len(candidate) >= 4:
                variants.add(candidate)
            if "%" in candidate:
                lowered = _PERCENT_HEX_RE.sub(lambda match: match.group(0).lower(), candidate)
                if len(lowered) >= 4:
                    variants.add(lowered)
    return tuple(sorted(variants, key=lambda item: (-len(item), item)))


def _known_secret_pattern(variants: tuple[str, ...]) -> re.Pattern[str] | None:
    if not variants:
        return None
    return re.compile("|".join(re.escape(item) for item in variants))


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans.sort(key=lambda span: (span[0], -span[1]))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _redact_text_before_cut(full_text: str, *, cut_index: int, variants: tuple[str, ...]) -> str:
    """Redact known secrets and pattern-matched tokens on ``full_text``, then cut at ``cut_index``.

    Any match starting before ``cut_index`` (even if it extends past ``cut_index``)
    is replaced with ``[REDACTED]`` and nothing past ``cut_index`` is emitted, so
    an agent-chosen ``max_response_bytes`` cannot slice an echoed secret in half.
    """

    spans: list[tuple[int, int]] = []
    known_pattern = _known_secret_pattern(variants)
    if known_pattern is not None:
        spans.extend(match.span() for match in known_pattern.finditer(full_text))
    spans.extend(match.span() for match in SECRET_VALUE_RE.finditer(full_text))
    if not spans:
        return full_text[:cut_index]
    parts: list[str] = []
    cursor = 0
    for start, end in _merge_spans(spans):
        if start >= cut_index:
            break
        if start > cursor:
            parts.append(full_text[cursor:start])
        parts.append("[REDACTED]")
        cursor = end
        if cursor >= cut_index:
            break
    if cursor < cut_index:
        parts.append(full_text[cursor:cut_index])
    return "".join(parts)


def _read_api_response_text(
    response: Any,
    *,
    charset: str,
    max_bytes: int,
    secret_values: list[str],
) -> tuple[str, bool, bool]:
    """Read up to ``max_bytes`` plus a secret-sized lookahead; redact before cutting when truncated."""

    try:
        decoder_factory = codecs.getincrementaldecoder(charset)
        encoding = charset
    except LookupError:
        decoder_factory = codecs.getincrementaldecoder("utf-8")
        encoding = "utf-8"
    variants = _expand_secret_variants(secret_values)
    max_secret_bytes = max((len(item.encode(encoding, errors="ignore")) for item in variants), default=0)
    lookahead_bytes = max(max_secret_bytes, _REDACTION_LOOKAHEAD_MIN_BYTES)
    raw = response.read(max_bytes + lookahead_bytes)
    truncated = len(raw) > max_bytes
    if not truncated:
        return raw.decode(encoding, errors="replace"), False, False
    bounded = raw[: max_bytes + lookahead_bytes]
    decoder = decoder_factory(errors="replace")
    prefix_text = decoder.decode(bounded[:max_bytes], final=False)
    suffix_text = decoder.decode(bounded[max_bytes:], final=True)
    full_text = prefix_text + suffix_text
    redacted = _redact_text_before_cut(full_text, cut_index=len(prefix_text), variants=variants)
    return redacted, True, True


def _redact_known_secret_values(
    value: Any,
    secret_values: list[str],
    *,
    _pattern: re.Pattern[str] | None = None,
    _prepared: bool = False,
) -> Any:
    pattern = _pattern if _prepared else _known_secret_pattern(_expand_secret_variants(secret_values))
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact_known_secret_values(item, secret_values, _pattern=pattern, _prepared=True) for item in value]
    if isinstance(value, dict):
        return {
            (pattern.sub("[REDACTED]", key) if isinstance(key, str) else key): _redact_known_secret_values(
                child, secret_values, _pattern=pattern, _prepared=True
            )
            for key, child in value.items()
        }
    return value


def _redact_known_secret_text(value: str, secret_values: list[str]) -> str:
    pattern = _known_secret_pattern(_expand_secret_variants(secret_values))
    if pattern is None:
        return value
    return pattern.sub("[REDACTED]", value)


def signup_prepare(params: Mapping[str, Any]) -> ToolResult:
    request_payload = dict(params)
    return ToolResult(
        status="success",
        content={
            "status": "signup_prepared",
            "requires_user_approval": True,
            "creates_form": False,
            "next_step": "After user approval, return action_type=secure_credential_collection_request or action_type=signup_automation_request through the gateway.",
            "credential_key": _credential_key(request_payload.get("credential_key")),
            "service_name": _string(request_payload.get("service_name")),
            "signup_url": _string(request_payload.get("signup_url")),
            "username": _string(request_payload.get("username") or request_payload.get("email")),
            "password_field_ref": _string(request_payload.get("password_field_ref") or "password"),
        },
    )


def signup_execute_approved(params: Mapping[str, Any]) -> ToolResult:
    boundary = detect_manual_signup_boundary(params)
    if boundary:
        return ToolResult(
            status="success",
            content={
                "status": "manual_signup_required",
                "boundary_reason": boundary,
                "credential_key": _credential_key(params.get("credential_key")),
                "manual_fallback_available": True,
            },
        )
    # The full browser automation executor is intentionally separate from this broker action.
    # Until a registered automation executor is configured, fail into the secure manual path.
    return ToolResult(
        status="success",
        content={
            "status": "manual_signup_required",
            "boundary_reason": "signup_automation_executor_unavailable",
            "credential_key": _credential_key(params.get("credential_key")),
            "manual_fallback_available": True,
        },
    )


def detect_manual_signup_boundary(params: Mapping[str, Any]) -> str | None:
    if params.get("manual_required") or params.get("force_manual_fallback"):
        return "manual_signup_requested"
    haystack = json.dumps(params, sort_keys=True).lower()
    for marker in SIGNUP_BOUNDARY_MARKERS:
        if marker in haystack:
            return marker.replace(" ", "_")
    return None


def execute_credential_catalog_action(action: ValidatedAction, context: ToolContext) -> ToolResult:
    params = dict(action.params or {})
    try:
        if action.action == "credential.collect":
            raw_request = params.get("request")
            if params.get("expires_at") is not None or (
                isinstance(raw_request, Mapping) and raw_request.get("expires_at") is not None
            ):
                return ToolResult(
                    status="error",
                    error=(
                        "Temporary credential expiry can only be chosen by the person on the secure "
                        "broker form, not set directly through credential.collect."
                    ),
                    content={"error_code": CREDENTIAL_EXPIRY_FORM_ONLY},
                )
            request_payload = dict(raw_request or params)
            submitted_fields = dict(params.get("submitted_fields") or params.get("fields_values") or {})
            return collect_credential(
                request_payload,
                {str(k): str(v) for k, v in submitted_fields.items()},
                context=context,
            )
        if action.action == "credential.register_existing_source":
            return register_existing_source(params, context=context)
        if action.action == "credential.get_metadata":
            return get_credential_metadata(
                str(params.get("credential_key") or params.get("key") or ""),
                context=context,
            )
        if action.action == "credential.bind_for_openshell":
            return bind_for_openshell(params, context=context)
        if action.action == "credential.release_openshell_binding":
            return release_openshell_binding(params, context=context)
        if action.action == "credential.api_request":
            return api_request(params, context=context)
        if action.action == "template.list":
            return list_credential_templates(params)
        if action.action == "template.get":
            return get_credential_template(params)
        if action.action == "template.render_request":
            return render_credential_template_request(params)
        if action.action == "template.save_approved_variant":
            return save_approved_template_variant(params)
        if action.action == "signup.prepare":
            return signup_prepare(params)
        if action.action == "signup.execute_approved":
            return signup_execute_approved(params)
        if action.action == "signup.manual_fallback_session":
            return ToolResult(
                status="error",
                error="signup.manual_fallback_session is available through the broker HTTP session API, not secure_cli.",
            )
    except Exception as exc:
        return ToolResult(status="error", error=f"credential_catalog.{action.action} failed: {exc}")
    return ToolResult(status="error", error=f"Unsupported credential_catalog action: {action.action}")


def execute_secure_requests_action(action: ValidatedAction, context: ToolContext) -> ToolResult:
    params = dict(action.params or {})
    if action.action != "request":
        # The action name is the method; a different params.method used to win,
        # so 'get' could send DELETE.
        fixed_method = action.action.upper()
        requested = _string(params.get("method")).upper()
        if requested and requested != fixed_method:
            return ToolResult(
                status="error",
                error=(
                    f"secure_requests.{action.action} always sends {fixed_method}; use "
                    "secure_requests.request to choose another method."
                ),
                content={"error_code": "api_request_method_mismatch", "method": requested},
            )
        params["method"] = fixed_method
    return api_request(params, context=context)
